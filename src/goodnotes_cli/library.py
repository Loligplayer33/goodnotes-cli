"""Public read-only facade for a marked Goodnotes library snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, overload

from .errors import GoodnotesError
from .models import Item, Page, PageText, RecognitionStatus, SearchHit
from .presentation.coverage import format_search_coverage
from .reader.attachments import AttachmentResolver
from .reader.catalog import Catalog
from .reader.coverage import CoverageReader
from .reader.pages import PageReader
from .reader.pdf import PdfTextReader
from .reader.read import PageTextReader
from .reader.scope import ScopeResolver
from .reader.search import SearchReader
from .reader.session import ReaderSession

if TYPE_CHECKING:
    from .reader.coverage import LibraryStats, ScopedStats
    from .storage.sandbox import Sandbox


class Library:
    """Read a sandboxed Goodnotes snapshot without changing either database."""

    def __init__(self, sandbox: Sandbox, *, root_folder_id: str | None = None) -> None:
        self.sandbox = sandbox
        self.root_folder_id = root_folder_id
        self._session = ReaderSession(sandbox)
        self._make_collaborators()

    def _make_collaborators(self) -> None:
        self._catalog = Catalog(self._session, self.root_folder_id)
        self._attachments = AttachmentResolver(self.sandbox.attachments_dir)
        self._pages = PageReader(self._session, self._attachments)
        self._scope = ScopeResolver(self._catalog)
        self._pdf = PdfTextReader(self._session, self._pages, self._attachments)
        self._reader = PageTextReader(self._session, self._catalog, self._pages, self._pdf)
        self._coverage = CoverageReader(
            self._session, self._catalog, self._pages, self._scope, self._pdf, self._reader
        )
        self._search = SearchReader(
            self._session, self._catalog, self._pages, self._scope, self._pdf
        )

    def __enter__(self) -> Library:
        if self._session.is_open:
            raise GoodnotesError("Library is already open.")
        self._session = ReaderSession(self.sandbox)
        self._make_collaborators()
        try:
            self._session.open()
            self._catalog.load()
        except BaseException:
            try:
                self._session.close()
            finally:
                self._make_collaborators()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self._session.close()
        finally:
            self._make_collaborators()

    def list_items(self, folder: str | Item = "/", kind: str | None = None) -> list[Item]:
        return self._catalog.list_items(folder, kind)

    def tree(self, folder: str | Item = "/", max_depth: int | None = None) -> list[Item]:
        return self._catalog.tree(folder, max_depth)

    def resolve(self, selector: str | Item, kind: str | None = None) -> Item:
        return self._catalog.resolve(selector, kind)

    def pages(self, notebook: str | Item) -> list[Page]:
        self._session.require_open()
        self._catalog.ensure_consistent_graph()
        return self._pages.pages(self._catalog.resolve(notebook, kind="notebook").id)

    def read_page(
        self, notebook: str | Item, page: int, *, source: str = "all", max_chars: int | None = None
    ) -> PageText:
        return self._reader.read_page(notebook, page, source=source, max_chars=max_chars)

    def recognition_status(self, notebook: str | Item) -> dict[str, RecognitionStatus]:
        return self._coverage.recognition_status(notebook)

    def text_warnings(self) -> list[str]:
        return self._coverage.text_warnings()

    def search_coverage(
        self, folder: str | Item = "/", source: str = "all", *, notebook: str | Item | None = None
    ) -> dict[str, Any]:
        return format_search_coverage(
            self._coverage.search_coverage(folder, source, notebook=notebook)
        )

    def search(
        self,
        query: str,
        folder: str | Item = "/",
        source: str = "all",
        limit: int = 20,
        *,
        notebook: str | Item | None = None,
    ) -> list[SearchHit]:
        return self._search.search(query, folder, source, limit, notebook=notebook)

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
        self._session.require_open()
        self._catalog.ensure_consistent_graph()
        self._pdf.validate_scan(query, limit=limit, page_limit=page_limit, page_offset=page_offset)
        scope, notebooks, _ = self._scope.scope(folder, notebook)
        with self._pdf.pdf_session():
            return self._pdf.scan_pdfs(
                query, scope, notebooks, limit=limit, page_limit=page_limit, page_offset=page_offset
            )

    @overload
    def stats(self, notebook: None = None) -> LibraryStats: ...

    @overload
    def stats(self, notebook: str | Item) -> ScopedStats: ...

    def stats(self, notebook: str | Item | None = None) -> LibraryStats | ScopedStats:
        return self._coverage.stats(notebook)
