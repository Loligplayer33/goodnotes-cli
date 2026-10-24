"""Read-only access to a marked Goodnotes library snapshot."""

from __future__ import annotations

import hashlib
import heapq
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pymupdf

from .errors import GoodnotesError
from .models import Item, Page, PageText, SearchHit, TextPart
from .safety import open_database

if TYPE_CHECKING:
    from .safety import Sandbox


_FOLDER = 0
_NOTEBOOK = 1
_TEXT_TABLES = {
    "handwriting": ("annotation_text_content", "Handwriting"),
    "textbox": ("textbox_text_content", "Textbox"),
    "pdf": ("pdf_text_content", "PDF"),
}
_MAX_PDF_FALLBACK_BYTES = 128 * 1024 * 1024
_MAX_PDF_FALLBACK_TEXT = 100_000


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
        self._page_numbers: dict[tuple[str, str], int] = {}
        self._attachment_basename_cache: dict[str, list[Path]] | None = None
        self._text_warnings_cache: list[str] | None = None

    def __enter__(self) -> Library:
        if self._stack is not None:
            raise GoodnotesError("Library is already open.")

        stack = ExitStack()
        try:
            self._projection = stack.enter_context(open_database(self.sandbox.projection_path))
            if self.sandbox.fts_path is not None:
                fts_path = Path(self.sandbox.fts_path)
                if fts_path.is_file():
                    self._fts = stack.enter_context(open_database(fts_path))
            self._stack = stack
            self._register_casefold()
            self._load_catalog()
            self._text_warnings_cache = None
        except Exception:
            stack.close()
            self._projection = None
            self._fts = None
            self._stack = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stack is not None:
            self._stack.close()
        self._stack = None
        self._projection = None
        self._fts = None
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
        return [self._page_from_row(row, number) for number, row in enumerate(rows, start=1)]

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
        if source not in {"all", *_TEXT_TABLES}:
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
        selected_sources = list(_TEXT_TABLES) if source == "all" else [source]
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
                    "Handwriting text is machine-recognized and may be inaccurate; any `|` "
                    "alternatives are preserved verbatim."
                )

        if not truncated and "textbox" in selected_sources:
            rows, remaining, omitted, _ = self._read_text_table("textbox", selected.id, remaining)
            parts.extend(rows)
            truncated = truncated or omitted

        if not truncated and "pdf" in selected_sources:
            pdf_parts, remaining, omitted, indexed = self._read_pdf_text_for_page(
                selected.id, item.id, remaining
            )
            parts.extend(pdf_parts)
            truncated = truncated or omitted
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
        if source == "all":
            return self.text_warnings()
        label = _TEXT_TABLES[source][1]
        table = _TEXT_TABLES[source][0]
        if self._fts is None:
            return [
                f"The sandbox has no search index; indexed {label.lower()} text is unavailable."
            ]
        warnings: list[str] = []
        if not self._table_exists(self._fts, table):
            warnings.append(
                f"{label} text is unavailable because `{table}` is missing from the search index."
            )
        if source == "pdf" and not self._table_exists(self._fts, "page_meta"):
            warnings.append(
                "Indexed PDF text cannot be mapped because `page_meta` is missing from the "
                "search index."
            )
        return warnings

    def _read_text_table(
        self, source: str, page_id: str, budget: int | None
    ) -> tuple[list[TextPart], int | None, bool, bool]:
        """Read one ordinary content table while loading at most its remaining budget."""
        if self._fts is None:
            return [], budget, False, False
        table = _TEXT_TABLES[source][0]
        if not self._table_exists(self._fts, table):
            return [], budget, False, False

        if source == "handwriting":
            ordering = (
                "CASE priority WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 ELSE 2 END, text_id"
            )
            fields = "text_id, length(text) AS text_length, engine_type, priority"
        else:
            ordering = "text_id"
            fields = "text_id, length(text) AS text_length"
        parts: list[TextPart] = []
        found = False
        rows = self._fts.execute(
            f"""
            SELECT {fields}
            FROM {table}
            WHERE page_id = ? AND COALESCE(text, '') <> ''
            ORDER BY {ordering}
            """,
            (page_id,),
        )
        for row in rows:
            found = True
            if budget is not None and budget == 0:
                return parts, budget, True, found
            if budget is None:
                content = self._fts.execute(
                    f"SELECT text FROM {table} WHERE text_id = ?", (row["text_id"],)
                ).fetchone()
                text = content[0] if content is not None else ""
                part_truncated = False
            else:
                content = self._fts.execute(
                    f"SELECT substr(text, 1, ?) FROM {table} WHERE text_id = ?",
                    (budget + 1, row["text_id"]),
                ).fetchone()
                text = content[0] if content is not None else ""
                part_truncated = int(row["text_length"] or 0) > budget or len(text) > budget
                if part_truncated:
                    text = text[:budget]
            if text:
                parts.append(
                    TextPart(
                        source=source,
                        text=text,
                        engine=row["engine_type"] if source == "handwriting" else None,
                        priority=row["priority"] if source == "handwriting" else None,
                        truncated=part_truncated,
                    )
                )
            if budget is not None:
                budget = max(0, budget - len(text))
            if part_truncated:
                return parts, budget, True, found
        return parts, budget, False, found

    def text_warnings(self) -> list[str]:
        """Report unavailable text sources and index-to-page mapping limitations."""
        self._require_open()
        if self._text_warnings_cache is not None:
            return list(self._text_warnings_cache)
        if self._fts is None:
            self._text_warnings_cache = [
                "The sandbox has no search index; indexed handwriting, textbox, and PDF text "
                "sources are unavailable."
            ]
            return list(self._text_warnings_cache)

        warnings = [
            f"{label} text is unavailable because `{table}` is missing from the search index."
            for table, label in _TEXT_TABLES.values()
            if not self._table_exists(self._fts, table)
        ]
        if not self._table_exists(self._fts, "page_meta"):
            warnings.append(
                "Stored-index search cannot map text rows to notebook pages because `page_meta` "
                "is missing."
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
        self._text_warnings_cache = warnings
        return list(self._text_warnings_cache)

    def search(
        self,
        query: str,
        folder: str | Item = "/",
        source: str = "all",
        limit: int = 20,
    ) -> list[SearchHit]:
        """Find literal, case-folded substrings in ordinary SQLite content tables."""
        self._require_open()
        self._ensure_consistent_graph()
        if not isinstance(query, str) or not query:
            raise GoodnotesError("Search query must not be empty.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise GoodnotesError("Search limit must be a positive integer.")
        source = source.casefold() if isinstance(source, str) else ""
        valid_sources = {"all", "handwriting", "pdf", "textbox"}
        if source not in valid_sources:
            raise GoodnotesError("Search source must be all, handwriting, pdf, or textbox.")
        if self._fts is None:
            raise GoodnotesError("The sandbox has no search index.")

        selected_sources = list(_TEXT_TABLES) if source == "all" else [source]
        missing_sources = [
            item_source
            for item_source in selected_sources
            if not self._table_exists(self._fts, _TEXT_TABLES[item_source][0])
        ]
        if source != "all" and missing_sources:
            label = _TEXT_TABLES[source][1]
            raise GoodnotesError(f"{label} text is unavailable in this search index.")
        if source == "all" and len(missing_sources) == len(selected_sources):
            raise GoodnotesError("The search index has no available text content tables.")
        if not self._table_exists(self._fts, "page_meta") and source != "all":
            raise GoodnotesError(
                "Search results cannot be mapped because page metadata is missing."
            )

        parent_key = self._folder_key(folder, root_allowed=True)
        notebook_ids = self._notebook_ids_under(parent_key)
        if not notebook_ids:
            return []

        available = [
            item_source for item_source in selected_sources if item_source not in missing_sources
        ]
        if not available:
            raise GoodnotesError("The requested search index content table is unavailable.")

        source_order = {"handwriting": 0, "pdf": 1, "textbox": 2}
        candidates: list[_SearchCandidate] = []
        retained_keys: set[tuple[str, str, int, bytes]] = set()
        sequence = 0
        # Each source cursor orders by text_id, and a page/source fixes the other
        # sort fields. Thus a later exact duplicate can never outrank its first row;
        # keys for candidates outside the retained top-K set need not be kept.
        for item_source in available:
            for row in self._search_rows(item_source, query):
                document_id = row["document_id"]
                if document_id not in notebook_ids:
                    continue
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

        page_iterator = self._iter_notebook_pages(folder)
        for _ in range(page_offset):
            if next(page_iterator, None) is None:
                return {
                    "hits": [],
                    "pages_scanned": 0,
                    "next_page_offset": None,
                    "warnings": [
                        "PDF scan searches copied background PDF text only; it does not scan "
                        "handwriting or the stored search index."
                    ],
                }

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
        has_more = False
        folded_query = query.casefold()

        while pages_scanned < page_limit:
            candidate = next(page_iterator, None)
            if candidate is None:
                break
            notebook, page = candidate
            pages_scanned += 1
            text, extraction_warning, _ = self._extract_pdf_fallback(page)
            if not page.attachment_path:
                missing_backgrounds += 1
            elif page.pdf_page is None:
                unmapped_backgrounds += 1
            elif text is None:
                unreadable_backgrounds += 1
            if extraction_warning and "truncated at" in extraction_warning:
                truncated_pages += 1
            if text is not None and folded_query in text.casefold():
                hits.append(
                    SearchHit(
                        document_id=notebook.id,
                        path=notebook.path,
                        page=page.number,
                        page_id=page.id,
                        source="pdf",
                        text=text,
                    )
                )
                if len(hits) >= limit:
                    has_more = next(page_iterator, None) is not None
                    break

        if pages_scanned == page_limit and len(hits) < limit:
            has_more = next(page_iterator, None) is not None
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

        return {
            "hits": hits,
            "pages_scanned": pages_scanned,
            "next_page_offset": page_offset + pages_scanned if has_more else None,
            "warnings": warnings,
        }

    def _iter_notebook_pages(self, folder: str | Item) -> Iterator[tuple[Item, Page]]:
        for item in self.tree(folder):
            if item.kind == "notebook":
                for page in self.pages(item):
                    yield item, page

    def stats(self) -> dict[str, Any]:
        """Return counts and capability warnings suitable for a doctor/info command."""
        self._require_open()
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
        page_meta_ids: set[str] = set()
        if self._fts is not None and self._table_exists(self._fts, "page_meta"):
            page_meta_ids = {
                row[0] for row in self._fts.execute("SELECT page_id FROM page_meta").fetchall()
            }
        active_page_ids = set(self._page_numbers_by_id())
        pages_without_metadata = len(active_page_ids - page_meta_ids)
        metadata_without_pages = len(page_meta_ids - active_page_ids)
        if pages_without_metadata:
            warnings.append(f"{pages_without_metadata} active pages have no index metadata row.")
        if metadata_without_pages:
            warnings.append(f"{metadata_without_pages} index metadata rows have no active page.")
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
                self._fts is not None and fts_tables and self._table_exists(self._fts, "page_meta")
            ),
            "search_content_tables": fts_tables,
            "warnings": warnings,
        }

    def _require_open(self) -> None:
        if self._stack is None or self._projection is None:
            raise GoodnotesError("Open Library in a `with` block before reading it.")

    def _register_casefold(self) -> None:
        if self._fts is not None:
            self._fts.create_function(
                "casefold",
                1,
                lambda value: value.casefold() if isinstance(value, str) else "",
                deterministic=True,
            )

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def _load_catalog(self) -> None:
        assert self._projection is not None
        projection = self._projection
        required = ("folders", "documents", "folder_to_folder_items", "pages")
        if any(not self._table_exists(projection, name) for name in required):
            raise GoodnotesError(
                "The projection database does not contain the expected library tables."
            )

        folder_rows = projection.execute(
            "SELECT id, name, updated_at, deleted, position FROM folders"
        ).fetchall()
        document_rows = projection.execute(
            "SELECT id, name, updated_at, deleted, position FROM documents"
        ).fetchall()
        live_folders = {
            row["id"]: row for row in folder_rows if row["id"] and not bool(row["deleted"])
        }
        live_documents = {
            row["id"]: row for row in document_rows if row["id"] and not bool(row["deleted"])
        }
        self._active_items = {
            **{("folder", item_id): row["name"] for item_id, row in live_folders.items()},
            **{("notebook", item_id): row["name"] for item_id, row in live_documents.items()},
        }
        page_counts: dict[str, int] = {}
        self._page_numbers = {}
        for row in projection.execute(
            """
            SELECT id, document_id
            FROM pages
            WHERE COALESCE(deleted, 0) = 0
            ORDER BY document_id, position, id
            """
        ).fetchall():
            document_id = row["document_id"]
            if document_id not in live_documents:
                continue
            number = page_counts.get(document_id, 0) + 1
            page_counts[document_id] = number
            self._page_numbers[(document_id, row["id"])] = number

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
            if not item_id or item_id not in live_rows:
                any_rows = folder_rows if kind == "folder" else document_rows
                if any(record["id"] == item_id for record in any_rows):
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
            position = row["item_position"] or record["position"] or ""
            self._entries[key] = _Entry(
                kind=kind,
                id=item_id,
                name=record["name"],
                parent_id=parent_id,
                is_root=is_root,
                position=position,
                updated_at=float(record["updated_at"] or 0),
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

    def _page_from_row(self, row: sqlite3.Row, number: int) -> Page:
        assert self._projection is not None
        template_id = row["template_id"]
        template = None
        if template_id and self._table_exists(self._projection, "templates"):
            template = self._projection.execute(
                """
                SELECT attachment_id, page_number, size_width, size_height
                FROM templates WHERE id = ? AND document_id = ?
                ORDER BY id LIMIT 1
                """,
                (template_id, row["document_id"]),
            ).fetchone()
        annotation_count = 0
        if self._table_exists(self._projection, "notes_items") and row["group_id"]:
            count_row = self._projection.execute(
                "SELECT count(*) FROM notes_items WHERE group_id = ? AND COALESCE(deleted, 0) = 0",
                (row["group_id"],),
            ).fetchone()
            annotation_count = int(count_row[0])

        attachment_path: str | None = None
        attachment_id = template["attachment_id"] if template is not None else None
        if attachment_id and self._table_exists(self._projection, "attachments"):
            attachment = self._projection.execute(
                "SELECT path FROM attachments WHERE id = ? LIMIT 1", (attachment_id,)
            ).fetchone()
            if attachment is not None:
                safe_path = self._safe_attachment_path(attachment["path"])
                attachment_path = str(safe_path) if safe_path is not None else None

        pdf_page = None
        if template is not None and template["page_number"] is not None:
            try:
                candidate_page = int(template["page_number"])
                if candidate_page > 0:
                    pdf_page = candidate_page
            except (TypeError, ValueError):
                pdf_page = None
        if pdf_page is None:
            mapping = self._page_template_mapping(row["id"])
            if mapping is not None:
                pdf_page = mapping[1]

        return Page(
            id=row["id"],
            document_id=row["document_id"],
            number=number,
            position=row["position"],
            attachment_path=attachment_path,
            pdf_page=pdf_page,
            width=float(template["size_width"])
            if template is not None and template["size_width"] is not None
            else None,
            height=float(template["size_height"])
            if template is not None and template["size_height"] is not None
            else None,
            annotation_count=annotation_count,
        )

    def _safe_attachment_path(self, raw_path: str | None) -> Path | None:
        """Resolve an untrusted stored path and return it only inside the sandbox copy."""
        if not raw_path:
            return None
        try:
            root = Path(self.sandbox.attachments_dir).resolve()
            stored_path = Path(raw_path)
            candidate = stored_path if stored_path.is_absolute() else root / stored_path
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        if resolved.is_file() or stored_path.is_absolute():
            return resolved
        if ".." in stored_path.parts or not stored_path.name:
            return None
        matches = self._attachment_paths_by_basename().get(stored_path.name, [])
        return matches[0] if len(matches) == 1 else None

    def _attachment_paths_by_basename(self) -> dict[str, list[Path]]:
        if self._attachment_basename_cache is not None:
            return self._attachment_basename_cache
        root = Path(self.sandbox.attachments_dir).resolve()
        paths: dict[str, list[Path]] = {}
        for candidate in root.rglob("*"):
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.is_file():
                paths.setdefault(candidate.name, []).append(resolved)
        self._attachment_basename_cache = paths
        return paths

    def _extract_pdf_fallback(
        self, page: Page, *, max_chars: int | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract one mapped PDF page from a safe copied attachment, with size bounds."""
        if not page.attachment_path:
            return (
                None,
                "No confined copied attachment is available for PDF text extraction.",
                False,
            )
        if page.pdf_page is None or page.pdf_page < 1:
            return (
                None,
                "PDF text is not indexed and the page has no verified PDF page mapping.",
                False,
            )
        safe_path = self._safe_attachment_path(page.attachment_path)
        if safe_path is None:
            return None, "The stored attachment path is outside the sandbox copy.", False
        if not safe_path.is_file():
            return None, "The copied PDF attachment is missing.", False
        try:
            if safe_path.stat().st_size > _MAX_PDF_FALLBACK_BYTES:
                return (
                    None,
                    "The copied PDF attachment exceeds the safe extraction size limit.",
                    False,
                )
            with pymupdf.open(safe_path) as pdf:
                if page.pdf_page > pdf.page_count:
                    return None, "The mapped PDF page is outside the copied attachment.", False
                text = pdf.load_page(page.pdf_page - 1).get_text("text").strip()
        except (OSError, RuntimeError, ValueError, pymupdf.FileDataError):
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
            warning = "PDF text was extracted from the confined copied attachment and truncated "
            "to the requested character budget."
        return text, warning, truncated

    def _page_template_mapping(self, page_id: str) -> tuple[str, int] | None:
        if self._fts is None or not self._table_exists(self._fts, "page_meta"):
            return None
        row = self._fts.execute(
            "SELECT template_attachment_id_and_page_no FROM page_meta WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if row is None or not row[0]:
            return None
        attachment_id, separator, page_text = row[0].rpartition("_")
        if not separator:
            return None
        try:
            page_number = int(page_text)
        except (TypeError, ValueError):
            return None
        if page_number < 1:
            return None
        return attachment_id, page_number

    def _read_pdf_text_for_page(
        self, page_id: str, document_id: str, budget: int | None
    ) -> tuple[list[TextPart], int | None, bool, bool]:
        """Read PDF index rows, loading no more than the remaining page text budget."""
        if (
            self._fts is None
            or not self._table_exists(self._fts, "pdf_text_content")
            or not self._table_exists(self._fts, "page_meta")
        ):
            return [], budget, False, False

        mapping = self._fts.execute(
            "SELECT pdf_text_id, template_attachment_id_and_page_no "
            "FROM page_meta WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if mapping is None:
            return [], budget, False, False
        if mapping["pdf_text_id"] is not None:
            rows = self._fts.execute(
                """
                SELECT text_id, length(text) AS text_length
                FROM pdf_text_content
                WHERE text_id = ? AND COALESCE(text, '') <> ''
                ORDER BY text_id
                """,
                (mapping["pdf_text_id"],),
            )
        elif mapping["template_attachment_id_and_page_no"] and self._table_exists(
            self._fts, "template_meta"
        ):
            rows = self._fts.execute(
                """
                SELECT DISTINCT content.text_id, length(content.text) AS text_length
                FROM template_meta AS template
                JOIN pdf_text_content AS content
                  ON content.attachment_id = template.attachment_id
                 AND content.pdf_page_no = template.pdf_page_no
                 AND content.document_id = ?
                WHERE template.attachment_id || '_' || template.pdf_page_no = ?
                  AND COALESCE(content.text, '') <> ''
                ORDER BY content.text_id
                """,
                (document_id, mapping["template_attachment_id_and_page_no"]),
            )
        else:
            return [], budget, False, False

        parts: list[TextPart] = []
        found = False
        for row in rows:
            found = True
            if budget is not None and budget == 0:
                return parts, budget, True, found
            if budget is None:
                content = self._fts.execute(
                    "SELECT text FROM pdf_text_content WHERE text_id = ?", (row["text_id"],)
                ).fetchone()
                text = content[0] if content is not None else ""
                part_truncated = False
            else:
                content = self._fts.execute(
                    "SELECT substr(text, 1, ?) FROM pdf_text_content WHERE text_id = ?",
                    (budget + 1, row["text_id"]),
                ).fetchone()
                text = content[0] if content is not None else ""
                part_truncated = int(row["text_length"] or 0) > budget or len(text) > budget
                if part_truncated:
                    text = text[:budget]
            if text:
                parts.append(TextPart(source="pdf", text=text, truncated=part_truncated))
            if budget is not None:
                budget = max(0, budget - len(text))
            if part_truncated:
                return parts, budget, True, found
        return parts, budget, False, found

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

    def _search_rows(self, source: str, query: str) -> Iterator[sqlite3.Row]:
        assert self._fts is not None
        if not self._table_exists(self._fts, "page_meta"):
            return
        if source == "handwriting":
            if not self._table_exists(self._fts, "annotation_text_content"):
                return
            yield from self._fts.execute(
                """
                SELECT p.document_id, a.page_id, a.text, a.text_id AS sort_id
                FROM annotation_text_content AS a
                JOIN page_meta AS p ON p.page_id = a.page_id
                WHERE instr(casefold(a.text), casefold(?)) > 0
                ORDER BY a.text_id
                """,
                (query,),
            )
            return
        if source == "textbox":
            if not self._table_exists(self._fts, "textbox_text_content"):
                return
            yield from self._fts.execute(
                """
                SELECT p.document_id, t.page_id, t.text, t.text_id AS sort_id
                FROM textbox_text_content AS t
                JOIN page_meta AS p ON p.page_id = t.page_id
                WHERE instr(casefold(t.text), casefold(?)) > 0
                ORDER BY t.text_id
                """,
                (query,),
            )
            return
        if not self._table_exists(self._fts, "pdf_text_content"):
            return
        yield from self._fts.execute(
            """
            SELECT p.document_id, p.page_id, content.text, content.text_id AS sort_id
            FROM page_meta AS p
            JOIN pdf_text_content AS content ON content.text_id = p.pdf_text_id
            WHERE instr(casefold(content.text), casefold(?)) > 0
            ORDER BY content.text_id
            """,
            (query,),
        )
        if self._table_exists(self._fts, "template_meta"):
            yield from self._fts.execute(
                """
                SELECT DISTINCT p.document_id, p.page_id, content.text,
                                content.text_id AS sort_id
                FROM page_meta AS p
                JOIN template_meta AS template
                  ON p.template_attachment_id_and_page_no =
                     template.attachment_id || '_' || template.pdf_page_no
                JOIN pdf_text_content AS content
                  ON content.attachment_id = template.attachment_id
                 AND content.pdf_page_no = template.pdf_page_no
                 AND content.document_id = p.document_id
                WHERE p.pdf_text_id IS NULL
                  AND instr(casefold(content.text), casefold(?)) > 0
                ORDER BY content.text_id
                """,
                (query,),
            )

    def _page_number_for_id(self, document_id: str, page_id: str) -> int | None:
        return self._page_numbers.get((document_id, page_id))

    def _page_numbers_by_id(self) -> dict[str, int]:
        """Return active page ids for checking page-index metadata consistency."""
        return {page_id: number for (_, page_id), number in self._page_numbers.items()}

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
