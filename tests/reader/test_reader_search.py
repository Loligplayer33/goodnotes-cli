"""Exercise search collaborators with generated snapshot data."""

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.reader.attachments import AttachmentResolver
from goodnotes_cli.reader.catalog import Catalog
from goodnotes_cli.reader.pages import PageReader
from goodnotes_cli.reader.pdf import PdfTextReader
from goodnotes_cli.reader.scope import ScopeResolver
from goodnotes_cli.reader.search import SearchReader
from goodnotes_cli.reader.session import ReaderSession
from tests.support.library import _create_snapshot


def test_search_and_scope_collaborators(tmp_path):
    sandbox = _create_snapshot(tmp_path / "snapshot")
    session = ReaderSession(sandbox)
    session.open()
    try:
        catalog = Catalog(session)
        catalog.load()
        attachments = AttachmentResolver(sandbox.attachments_dir)
        pages = PageReader(session, attachments)
        pdf = PdfTextReader(session, pages, attachments)
        scope = ScopeResolver(catalog)
        reader = SearchReader(session, catalog, pages, scope, pdf)
        hit = reader.search("STRASSE", source="handwriting")[0]
        assert (hit.text, hit.page, hit.source) == ("Straße | Strasse", 1, "handwriting")
        assert reader.search("recognized", folder="Shared B") == []
        assert reader.search("direct PDF phrase", source="pdf")[0].page == 1
        with pytest.raises(GoodnotesError, match="--notebook"):
            scope.scope("doc-root", None)
        with pytest.raises(GoodnotesError, match="either"):
            scope.scope("Shared A", "doc-root")
    finally:
        session.close()
