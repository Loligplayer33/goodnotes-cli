"""Snapshot and text-index coverage diagnostics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, overload

from ..errors import GoodnotesError
from ..models import Item, RecognitionStatus
from . import index
from . import text as text_queries
from .catalog import Catalog
from .pages import PageReader
from .pdf import PdfTextReader
from .read import PageTextReader
from .scope import ScopeResolver
from .session import ReaderSession

_FOLDER = 0
_NOTEBOOK = 1


@dataclass(frozen=True)
class CoverageRecommendation:
    kind: Literal["scan", "refresh", "inspect_pending", "read"]
    scope_option: str | None = None
    scope_value: str | None = None


class CoverageResult(TypedDict):
    scope: dict[str, Any]
    selected_source: str
    active_pages: int
    page_meta: text_queries.PageMetaDiagnostics
    sources: dict[str, text_queries.SourceCoverage]
    completeness: Literal["unknown"]
    warnings: list[str]
    recommendations: list[CoverageRecommendation]


class PageIndexCounts(TypedDict):
    active_pages: int
    page_meta_available: bool
    pages_without_index_metadata: int | None
    index_metadata_without_active_pages: int | None
    interpretation: str
    warnings: list[str]


class ReferenceCounts(TypedDict):
    availability: Literal["available", "unavailable"]
    distinct_document_ids: int | None
    active_document_matches: int | None
    visible_active_document_matches: int | None
    unreachable_active_document_matches: int | None
    not_active_document_references: int | None
    unusable_reference_values: int | None


class CatalogReferenceCounts(TypedDict):
    scope: Literal["all-catalog-references"]
    template_library_document_id: ReferenceCounts
    pattern_library_document_id: ReferenceCounts


class LibraryStats(TypedDict):
    library_root: str | None
    active_folders: int
    active_notebooks: int
    active_pages: int
    pages_outside_active_notebooks: int
    deleted_folders: int
    deleted_notebooks: int
    visible_folders: int
    visible_notebooks: int
    orphaned_folders: int
    orphaned_notebooks: int
    orphaned_items: int
    broken_links: int
    deleted_link_rows: int
    links_to_deleted_items: int
    duplicate_parent_items: int
    cycle_folders: int
    pages_without_index_metadata: int | None
    index_metadata_without_active_pages: int | None
    search_index_available: bool
    search_content_tables: list[str]
    catalog_reference_counts: CatalogReferenceCounts
    diagnostic_notes: list[str]
    warnings: list[str]


class ScopedStats(TypedDict):
    scope: dict[str, str]
    page_index: PageIndexCounts
    library: LibraryStats
    warnings: list[str]


class CoverageReader:
    def __init__(
        self,
        session: ReaderSession,
        catalog: Catalog,
        pages: PageReader,
        scope: ScopeResolver,
        pdf: PdfTextReader,
        reader: PageTextReader,
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._pages = pages
        self._scope = scope
        self._pdf = pdf
        self._reader = reader
        self.root_folder_id = catalog.root_folder_id
        self._text_warnings_cache: list[str] | None = None

    def recognition_status(self, notebook: str | Item) -> dict[str, RecognitionStatus]:
        """Return stored handwriting-recognition status for every active notebook page."""
        self._session.require_open()
        self._catalog.ensure_consistent_graph()
        item = self._scope.resolve_notebook(notebook, option="--notebook")
        assert self._session.projection is not None
        page_ids = [
            row[0]
            for row in self._session.projection.execute(
                "SELECT id FROM pages WHERE document_id = ? AND COALESCE(deleted, 0) = 0 "
                "ORDER BY position, id",
                (item.id,),
            )
        ]
        return self._reader._recognition_status_for_page_ids(page_ids)

    def text_warnings(self) -> list[str]:
        """Report library-wide text-source and page-mapping limitations for diagnostics."""
        self._session.require_open()
        self._session.ensure_fts()
        if self._text_warnings_cache is not None:
            return list(self._text_warnings_cache)
        if self._session.fts is None:
            self._text_warnings_cache = [
                "Library-wide: the sandbox has no search index; indexed handwriting, textbox, "
                "and PDF text sources are unavailable."
            ]
            return list(self._text_warnings_cache)

        warnings = [
            f"{index.TEXT_LABELS[source]} text is unavailable because `{table}` is missing "
            "from the search index."
            for source, table in index.TEXT_TABLES.items()
            if not self._session.table_exists(self._session.fts, table)
        ]
        if not index.page_meta_supports_search(self._session.fts, schema=self._session.fts_schema):
            warnings.append(
                "Stored-index search cannot map text rows to notebook pages because usable "
                "`page_meta` columns are missing."
            )
        else:
            index_page_ids = {
                row[0]
                for row in self._session.fts.execute("SELECT page_id FROM page_meta").fetchall()
            }
            missing_page_count = len(
                set(self._pages.page_numbers_by_id(self._catalog.active_items_by_kind("notebook")))
                - index_page_ids
            )
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
    ) -> CoverageResult:
        """Summarize stored text and mapping metadata for one resolved library scope."""
        self._session.require_open()
        self._catalog.ensure_consistent_graph()
        self._session.ensure_fts()
        source = source.casefold() if isinstance(source, str) else ""
        try:
            requested_sources = index.selected_text_sources(source)
        except GoodnotesError as exc:
            raise GoodnotesError(
                "Coverage source must be all, text, handwriting, pdf, or textbox."
            ) from exc
        scope, _, notebook_ids = self._scope.scope(folder, notebook)
        page_refs = self._pages.page_refs_for_notebooks(notebook_ids)
        pdf_bindings = (
            self._pdf.bindings_for(page_refs)
            if "pdf" in requested_sources and self._session.fts is not None
            else None
        )
        details = text_queries.coverage(
            self._session.fts,
            page_refs,
            list(requested_sources),
            schema=self._session.fts_schema,
            pdf_bindings=pdf_bindings,
        )
        mapping = details["page_meta"]
        source_details = details["sources"]
        warnings = list(details["warnings"])
        recommendations: list[CoverageRecommendation] = []

        if "pdf" in requested_sources:
            if scope["type"] == "notebook":
                scope_option = "--notebook"
                scope_value = scope["path"]
            elif scope["folder_id"] is None:
                scope_option = "--folder"
                scope_value = "/"
            else:
                scope_option = "--folder"
                scope_value = scope["path"]
            recommendations.append(CoverageRecommendation("scan", scope_option, scope_value))
        if not mapping["available"] or mapping["active_pages_without_metadata"]:
            recommendations.append(CoverageRecommendation("refresh"))
        if mapping.get("stale_or_mismatched_pages", 0):
            recommendations.append(CoverageRecommendation("refresh"))
        for source_details_row in source_details.values():
            if source_details_row["availability"] == "unavailable":
                recommendations.append(CoverageRecommendation("refresh"))

        if "handwriting" in requested_sources:
            recognition = self._reader._recognition_status_for_page_ids(
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
                recommendations.append(CoverageRecommendation("inspect_pending"))

        warnings.append(
            "Stored-text counts describe this snapshot; source-index completeness cannot be "
            "confirmed from the available metadata."
        )
        if not recommendations:
            recommendations.append(CoverageRecommendation("read"))
        return {
            "scope": scope,
            "selected_source": source,
            "active_pages": len(page_refs),
            "page_meta": mapping,
            "sources": source_details,
            "completeness": "unknown",
            "warnings": list(dict.fromkeys(warnings)),
            "recommendations": list(dict.fromkeys(recommendations))[:5],
        }

    @overload
    def stats(self, notebook: None = None) -> LibraryStats: ...

    @overload
    def stats(self, notebook: str | Item) -> ScopedStats: ...

    def stats(self, notebook: str | Item | None = None) -> LibraryStats | ScopedStats:
        """Return library diagnostics or page-index diagnostics for one notebook."""
        self._session.require_open()
        self._session.ensure_fts()
        assert self._session.projection is not None
        active_folders = self._catalog.active_folder_count
        active_notebooks = self._catalog.active_notebook_count
        visible_folders = sum(
            1
            for key, path in self._catalog.paths.items()
            if key[0] == "folder" and path is not None
        )
        visible_notebooks = sum(
            1
            for key, path in self._catalog.paths.items()
            if key[0] == "notebook" and path is not None
        )
        page_index = self._page_index_counts()
        page_count = self._count(self._session.projection, "pages", "COALESCE(deleted, 0) = 0")
        deleted_folders = self._count(
            self._session.projection, "folders", "COALESCE(deleted, 0) <> 0"
        )
        deleted_notebooks = self._count(
            self._session.projection, "documents", "COALESCE(deleted, 0) <> 0"
        )
        deleted_link_rows = self._count(
            self._session.projection,
            "folder_to_folder_items",
            "COALESCE(deleted, 0) <> 0",
        )
        fts_tables = []
        if self._session.fts is not None:
            fts_tables = [
                name
                for name in (
                    "annotation_text_content",
                    "textbox_text_content",
                    "pdf_text_content",
                )
                if self._session.table_exists(self._session.fts, name)
            ]
        warnings: list[str] = []
        if self._session.fts is None:
            warnings.append("Search index file is missing from the sandbox.")
        elif len(fts_tables) != 3:
            warnings.append("One or more ordinary text content tables are missing from the index.")
        if self._catalog.orphan_count:
            warnings.append(
                f"{self._catalog.orphan_count} active items have no resolvable root path."
            )
        if self._catalog.broken_link_count:
            warnings.append(
                f"{self._catalog.broken_link_count} active catalog links point to missing "
                "targets or parents."
            )
        if self._catalog.duplicate_parent_count:
            warnings.append(
                f"{self._catalog.duplicate_parent_count} active items have multiple parent links."
            )
        if self._catalog.cycle_count:
            warnings.append(
                f"{self._catalog.cycle_count} active folders are part of a parent cycle."
            )
        if self._catalog.links_to_deleted_count:
            warnings.append(
                f"{self._catalog.links_to_deleted_count} active links to deleted items "
                "were ignored."
            )
        warnings.extend(page_index["warnings"])
        result: LibraryStats = {
            "library_root": self.root_folder_id,
            "active_folders": active_folders,
            "active_notebooks": active_notebooks,
            "active_pages": page_count,
            "pages_outside_active_notebooks": page_count - page_index["active_pages"],
            "deleted_folders": deleted_folders,
            "deleted_notebooks": deleted_notebooks,
            "visible_folders": visible_folders,
            "visible_notebooks": visible_notebooks,
            "orphaned_folders": max(0, active_folders - visible_folders),
            "orphaned_notebooks": max(0, active_notebooks - visible_notebooks),
            "orphaned_items": self._catalog.orphan_count,
            "broken_links": self._catalog.broken_link_count,
            "deleted_link_rows": deleted_link_rows,
            "links_to_deleted_items": self._catalog.links_to_deleted_count,
            "duplicate_parent_items": self._catalog.duplicate_parent_count,
            "cycle_folders": self._catalog.cycle_count,
            "pages_without_index_metadata": page_index["pages_without_index_metadata"],
            "index_metadata_without_active_pages": page_index[
                "index_metadata_without_active_pages"
            ],
            "search_index_available": bool(
                self._session.fts is not None and fts_tables and page_index["page_meta_available"]
            ),
            "search_content_tables": fts_tables,
            "catalog_reference_counts": self._library_document_reference_counts(),
            "diagnostic_notes": [
                "Unreachable active folders and notebooks remain catalog records; "
                "these counts do not establish deletion or content loss.",
                "Page-index counts compare (document_id, page_id) identities. Missing or extra "
                "metadata rows do not establish missing note text or data safe to delete.",
                "Template and pattern library-document references are contextual links; "
                "they do not establish system ownership of a notebook or folder.",
                "active_pages counts nondeleted page rows. Page-index coverage excludes pages "
                "outside active notebooks; their row count is reported separately.",
            ],
            "warnings": warnings,
        }
        if notebook is None:
            return result

        item = self._catalog.resolve(notebook, kind="notebook")
        scoped_page_index = self._page_index_counts(item.id)
        return {
            "scope": {"type": "notebook", "notebook_id": item.id, "path": item.path},
            "page_index": scoped_page_index,
            "library": result,
            "warnings": scoped_page_index["warnings"],
        }

    def _page_index_counts(self, document_id: str | None = None) -> PageIndexCounts:
        """Compare active projection pages with index metadata by document and page identity."""
        assert self._session.projection is not None
        page_filter = " AND document_id = ?" if document_id is not None else ""
        params: tuple[str, ...] = (document_id,) if document_id is not None else ()
        active_document_ids = self._catalog.active_items_by_kind("notebook")
        active_pages = {
            (row["document_id"], row["id"])
            for row in self._session.projection.execute(
                "SELECT document_id, id FROM pages WHERE COALESCE(deleted, 0) = 0" + page_filter,
                params,
            )
            if row["document_id"] in active_document_ids
        }
        available = index.page_meta_supports_search(
            self._session.fts, schema=self._session.fts_schema
        )
        warnings: list[str] = []
        if available and self._session.fts is not None:
            meta_filter = " WHERE document_id = ?" if document_id is not None else ""
            metadata_pages = {
                (row["document_id"], row["page_id"])
                for row in self._session.fts.execute(
                    "SELECT document_id, page_id FROM page_meta" + meta_filter,
                    params,
                )
                if self._catalog.root_item_ids is None
                or row["document_id"] in self._catalog.root_item_ids["documents"]
            }
            pages_without_metadata: int | None = len(active_pages - metadata_pages)
            metadata_without_active_pages: int | None = len(metadata_pages - active_pages)
            if pages_without_metadata:
                warnings.append(
                    f"{pages_without_metadata} active pages in this scope "
                    "have no index metadata row."
                )
            if metadata_without_active_pages:
                warnings.append(
                    f"{metadata_without_active_pages} index metadata identities "
                    "in this scope do not match an active page."
                )
        else:
            pages_without_metadata = None
            metadata_without_active_pages = None
            warnings.append("Page-to-index metadata coverage is unavailable for this scope.")
        return {
            "active_pages": len(active_pages),
            "page_meta_available": available,
            "pages_without_index_metadata": pages_without_metadata,
            "index_metadata_without_active_pages": metadata_without_active_pages,
            "interpretation": (
                "Counts compare (document_id, page_id) identities within active notebooks. "
                "Missing or extra metadata rows do not establish missing note text "
                "or data safe to delete."
            ),
            "warnings": warnings,
        }

    def _library_document_reference_counts(self) -> CatalogReferenceCounts:
        """Summarize template/pattern references without inferring notebook ownership."""
        assert self._session.projection is not None
        active_document_ids = {
            item_id for kind, item_id in self._catalog.active_items if kind == "notebook"
        }
        result: dict[str, ReferenceCounts] = {}
        for key, table, column in (
            ("template_library_document_id", "templates", "template_library_document_id"),
            ("pattern_library_document_id", "patterns", "pattern_library_document_id"),
        ):
            if not self._session.table_exists(
                self._session.projection, table
            ) or column not in self._session.table_columns(self._session.projection, table):
                result[key] = {
                    "availability": "unavailable",
                    "distinct_document_ids": None,
                    "active_document_matches": None,
                    "visible_active_document_matches": None,
                    "unreachable_active_document_matches": None,
                    "not_active_document_references": None,
                    "unusable_reference_values": None,
                }
                continue
            quoted_table = '"' + table.replace('"', '""') + '"'
            quoted_column = '"' + column.replace('"', '""') + '"'
            values = [
                row[0]
                for row in self._session.projection.execute(
                    f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
                    f"WHERE {quoted_column} IS NOT NULL"
                )
            ]
            references = {value for value in values if isinstance(value, str) and value}
            active_matches = references & active_document_ids
            visible_matches = {
                document_id
                for document_id in active_matches
                if self._catalog.paths.get(("notebook", document_id)) is not None
            }
            result[key] = {
                "availability": "available",
                "distinct_document_ids": len(references),
                "active_document_matches": len(active_matches),
                "visible_active_document_matches": len(visible_matches),
                "unreachable_active_document_matches": len(active_matches - visible_matches),
                "not_active_document_references": len(references - active_document_ids),
                "unusable_reference_values": len(values) - len(references),
            }
        return {
            "scope": "all-catalog-references",
            "template_library_document_id": result["template_library_document_id"],
            "pattern_library_document_id": result["pattern_library_document_id"],
        }

    def _count(self, connection: sqlite3.Connection, table: str, where: str) -> int:
        if not self._session.table_exists(connection, table):
            return 0
        params: tuple[str, ...] = ()
        if self.root_folder_id is not None:
            if table in {"folders", "documents", "pages"}:
                item_type = _FOLDER if table == "folders" else _NOTEBOOK
                column = "document_id" if table == "pages" else "id"
                where += (
                    f" AND {column} IN (SELECT item_id FROM folder_to_folder_items "
                    f"WHERE root_folder_id = ? AND item_type = {item_type})"
                )
            elif table == "folder_to_folder_items":
                where += " AND root_folder_id = ?"
            else:
                raise GoodnotesError("Unsupported count table for a selected library root.")
            params = (self.root_folder_id,)
        return int(
            connection.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]
        )
