"""Bounded literal search over a snapshot's text index."""

from __future__ import annotations

import hashlib
import heapq
import sqlite3
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass

from ..errors import GoodnotesError, TextIndexUnavailableError
from ..models import Item, SearchHit
from . import index
from . import text as text_queries
from .catalog import Catalog
from .pages import PageReader
from .pdf import PdfTextReader
from .scope import ScopeResolver
from .session import ReaderSession
from .validation import required_integer


@dataclass(frozen=True)
class _SearchCandidate:
    """A heap entry whose smallest heap element is the worst retained hit."""

    sort_key: tuple[str, int, int, int]
    sequence: int
    duplicate_key: tuple[str, str, int, bytes]
    hit: SearchHit

    def __lt__(self, other: _SearchCandidate) -> bool:
        return (self.sort_key, self.sequence) > (other.sort_key, other.sequence)


class SearchReader:
    """Search an open session using catalog, scope, page and PDF collaborators."""

    def __init__(
        self,
        session: ReaderSession,
        catalog: Catalog,
        pages: PageReader,
        scope: ScopeResolver,
        pdf: PdfTextReader,
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._pages = pages
        self._scope = scope
        self._pdf = pdf

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
        self._session.require_open()
        self._catalog.ensure_consistent_graph()
        if not isinstance(query, str) or not query:
            raise GoodnotesError("Search query must not be empty.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise GoodnotesError("Search limit must be a positive integer.")
        source = source.casefold() if isinstance(source, str) else ""
        try:
            selected_sources = index.selected_text_sources(source)
        except GoodnotesError as exc:
            raise GoodnotesError(
                "Search source must be all, text, handwriting, pdf, or textbox."
            ) from exc
        _, _, notebook_ids = self._scope.scope(folder, notebook)
        self._session.ensure_fts()
        if self._session.fts is None:
            raise TextIndexUnavailableError("The sandbox has no search index.")

        missing_sources = [
            item_source
            for item_source in selected_sources
            if not index.search_source_available(
                self._session.fts, item_source, schema=self._session.fts_schema
            )
        ]
        if source in index.TEXT_TABLES and missing_sources:
            label = index.TEXT_LABELS[source]
            raise TextIndexUnavailableError(f"{label} text is unavailable in this search index.")
        if source in {"all", "text"} and len(missing_sources) == len(selected_sources):
            raise TextIndexUnavailableError(
                "The search index has no available text content tables."
            )
        if not index.page_meta_supports_search(self._session.fts, schema=self._session.fts_schema):
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
        pdf_bindings: text_queries._PDFPageBindings | None = None
        if "pdf" in available:
            pdf_bindings = self._pdf.bindings_for(self._pages.page_refs_for_notebooks(notebook_ids))
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
        assert self._session.fts is not None
        search_batch_size = self._search_batch_size()
        notebook_batches = list(index.batches(sorted(notebook_ids), search_batch_size))
        # Each source cursor orders by text_id, and a page/source fixes the other
        # sort fields. Thus a later exact duplicate can never outrank its first row;
        # keys for candidates outside the retained top-K set need not be kept.
        page_numbers = self._pages.page_numbers(notebook_ids)
        for item_source in available:
            for notebook_batch in notebook_batches:
                if item_source == "pdf":
                    assert pdf_bindings is not None
                    rows = text_queries.search_pdf_rows(
                        self._session.fts, pdf_bindings, folded_query, notebook_batch
                    )
                else:
                    rows = self._search_rows(item_source, folded_query, notebook_batch)
                with closing(rows):
                    for row in rows:
                        document_id = row["document_id"]
                        page_id = row["page_id"]
                        page_number = page_numbers.get((document_id, page_id))
                        if page_number is None:
                            continue
                        entry = self._catalog.entries.get(("notebook", document_id))
                        path = self._catalog.paths.get(("notebook", document_id))
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
                            required_integer(
                                row["sort_id"], f"{index.TEXT_TABLES[item_source]}.text_id"
                            ),
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

    def _search_batch_size(self) -> int:
        assert self._session.fts is not None
        try:
            return index.sql_batch_size(self._session.fts, fixed=1)
        except GoodnotesError as exc:
            raise GoodnotesError("SQLite parameter limit is too low for text search.") from exc

    def _search_rows(
        self, source: str, query: str, document_ids: list[str] | None = None
    ) -> Generator[sqlite3.Row, None, None]:
        """Search handwriting or textbox rows for a bounded notebook-id set."""
        assert self._session.fts is not None
        if document_ids is None:
            document_ids = sorted(self._catalog.notebook_ids_under(None))
        batch_size = self._search_batch_size()
        if len(document_ids) > batch_size:
            for batch in index.batches(document_ids, batch_size):
                yield from self._search_rows(source, query, batch)
            return
        if not document_ids or not index.page_meta_supports_search(
            self._session.fts, schema=self._session.fts_schema
        ):
            return
        if source not in {"handwriting", "textbox"}:
            return
        table = index.TEXT_TABLES[source]
        if not self._session.table_exists(self._session.fts, table) or not {
            "page_id",
            "text_id",
            "text",
        } <= self._session.table_columns(self._session.fts, table):
            return
        alias = "a" if source == "handwriting" else "t"
        scope = ", ".join("?" for _ in document_ids)
        page_meta_columns = self._session.table_columns(self._session.fts, "page_meta")
        mapping_filter = index.page_meta_live_filter(page_meta_columns, alias="p")
        with closing(
            self._session.fts.execute(
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
        ) as cursor:
            # yield from would delegate a second cursor close on GeneratorExit.
            for row in cursor:  # noqa: UP028
                yield row
