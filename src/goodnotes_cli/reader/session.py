"""Ownership of read-only connections and PDF handles for a library session."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import GoodnotesError
from ..storage.paths import stat_key
from ..storage.sandbox import open_database
from .index import SchemaCache, table_columns, table_exists

if TYPE_CHECKING:
    from ..storage.sandbox import Sandbox

_MAX_OPEN_PDFS = 4


def _pymupdf() -> Any:
    """Import PyMuPDF only for operations that actually read a PDF."""
    import pymupdf

    return pymupdf


class _PDFDocumentCache:
    """A small LRU of read-only PDF documents scoped to one Library session."""

    def __init__(self, maximum: int = _MAX_OPEN_PDFS) -> None:
        self.maximum = maximum
        self.documents: OrderedDict[Path, tuple[Any, tuple[int, int, int, int, int, int]]] = (
            OrderedDict()
        )

    def get(self, path: Path) -> Any:
        identity = stat_key(path.stat())
        cached = self.documents.pop(path, None)
        document = None
        if cached is not None:
            document, previous_identity = cached
            if identity != previous_identity:
                document.close()
                document = None
        if document is None:
            document = _pymupdf().open(path)
        self.documents[path] = (document, identity)
        while len(self.documents) > self.maximum:
            _, (evicted, _) = self.documents.popitem(last=False)
            evicted.close()
        return document

    def close(self) -> None:
        documents = list(reversed(self.documents.values()))
        self.documents.clear()
        first_error: BaseException | None = None
        for document, _ in documents:
            try:
                document.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class ReaderSession:
    """Own every database and PDF handle acquired during one Library open."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.stack: ExitStack | None = None
        self.projection: sqlite3.Connection | None = None
        self.fts: sqlite3.Connection | None = None
        self.projection_schema: SchemaCache | None = None
        self.fts_schema: SchemaCache | None = None
        self.fts_attempted = False
        self.pdf_documents: _PDFDocumentCache | None = None
        self._fts_close: Any = None
        self._cleanup_errors: list[BaseException] = []

    @property
    def is_open(self) -> bool:
        return self.stack is not None and self.projection is not None

    def require_open(self) -> None:
        if not self.is_open:
            raise GoodnotesError("Open Library in a `with` block before reading it.")

    def schema_for(self, connection: sqlite3.Connection) -> SchemaCache:
        if connection is self.projection and self.projection_schema is not None:
            return self.projection_schema
        if connection is self.fts and self.fts_schema is not None:
            return self.fts_schema
        return SchemaCache(connection)

    def table_exists(self, connection: sqlite3.Connection | None, name: str) -> bool:
        return connection is not None and table_exists(
            connection, name, schema=self.schema_for(connection)
        )

    def table_columns(self, connection: sqlite3.Connection, name: str) -> set[str]:
        return table_columns(connection, name, schema=self.schema_for(connection))

    def open(self) -> None:
        if self.is_open:
            raise GoodnotesError("Library is already open.")
        stack = ExitStack()
        try:
            self.projection = stack.enter_context(open_database(self.sandbox.projection_path))
            self.projection_schema = SchemaCache(self.projection)
            stack.callback(self._close_index)
            stack.callback(self._close_pdfs)
            self.stack = stack
        except BaseException:
            stack.close()
            self.projection = None
            self.projection_schema = None
            raise

    def ensure_fts(self) -> sqlite3.Connection | None:
        if self.fts_attempted:
            return self.fts
        if not self.is_open:
            raise GoodnotesError("Open Library in a `with` block before reading it.")
        if self.sandbox.fts_path is None or not Path(self.sandbox.fts_path).is_file():
            self.fts_attempted = True
            return None
        assert self.stack is not None
        try:
            with ExitStack() as provisional:
                self.fts = provisional.enter_context(open_database(Path(self.sandbox.fts_path)))
                self.fts_schema = SchemaCache(self.fts)
                self._register_casefold()
                self._fts_close = provisional.pop_all().close
        except BaseException:
            self.fts = None
            self.fts_schema = None
            raise
        self.fts_attempted = True
        return self.fts

    def _register_casefold(self) -> None:
        assert self.fts is not None
        self.fts.create_function(
            "casefold",
            1,
            lambda value: value.casefold() if isinstance(value, str) else "",
            deterministic=True,
        )

    def start_pdf_cache(self) -> None:
        if self.pdf_documents is None:
            self.pdf_documents = _PDFDocumentCache()

    @contextmanager
    def pdf_document(self, path: Path) -> Generator[Any, None, None]:
        if self.pdf_documents is not None:
            yield self.pdf_documents.get(path)
            return
        document = _pymupdf().open(path)
        try:
            yield document
        finally:
            document.close()

    def _close_pdfs(self) -> None:
        if self.pdf_documents is not None:
            try:
                self.pdf_documents.close()
            except BaseException as exc:
                self._cleanup_errors.append(exc)

    def _close_index(self) -> None:
        if self._fts_close is not None:
            try:
                self._fts_close()
            except BaseException as exc:
                self._cleanup_errors.append(exc)

    def close(self) -> None:
        try:
            if self.stack is not None:
                self.stack.close()
        except BaseException as exc:
            self._cleanup_errors.append(exc)
        finally:
            self.pdf_documents = None
            self.stack = None
            self.projection = None
            self.fts = None
            self.projection_schema = None
            self.fts_schema = None
            self.fts_attempted = False
            self._fts_close = None
        errors, self._cleanup_errors = self._cleanup_errors, []
        if errors:
            if len(errors) > 1:
                raise errors[0] from errors[1]
            raise errors[0]
