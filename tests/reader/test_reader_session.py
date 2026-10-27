"""Generated reader-session failure and cleanup regressions."""

from pathlib import Path

import pytest

from goodnotes_cli.reader import session as session_module
from goodnotes_cli.reader.session import ReaderSession
from tests.support.library import _create_snapshot


def test_failed_index_schema_closes_connection_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ReaderSession(_create_snapshot(tmp_path))
    session.open()
    schema = session_module.SchemaCache
    failed = []

    def fail(connection):
        failed.append(connection)
        raise RuntimeError("schema failure")

    monkeypatch.setattr(session_module, "SchemaCache", fail)
    with pytest.raises(RuntimeError, match="schema failure"):
        session.ensure_fts()
    with pytest.raises(Exception, match="closed"):
        failed[0].execute("SELECT 1")
    assert session.fts is None
    monkeypatch.setattr(session_module, "SchemaCache", schema)
    assert session.ensure_fts() is not None
    session.close()


def test_cleanup_attempts_all_resources_and_preserves_first_failure(
    tmp_path: Path,
) -> None:
    session = ReaderSession(_create_snapshot(tmp_path))
    session.open()
    assert session.projection is not None
    projection = session.projection
    events = []

    class Document:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(self.name)
            raise RuntimeError(self.name)

    session.start_pdf_cache()
    assert session.pdf_documents is not None
    session.pdf_documents.documents.update(
        {Path(name): (Document(name), (0, 0, 0, 0, 0, 0)) for name in ("first", "second")}
    )

    def fail_index():
        events.append("index")
        raise ValueError("index failed")

    session._fts_close = fail_index
    with pytest.raises(RuntimeError, match="second") as error:
        session.close()
    assert isinstance(error.value.__cause__, ValueError)
    assert events == ["second", "first", "index"]
    with pytest.raises(Exception, match="closed"):
        projection.execute("SELECT 1")
    assert not session.is_open


def test_close_failure_retains_body_error_as_context(tmp_path: Path) -> None:
    from goodnotes_cli.library import Library

    class Document:
        def close(self):
            raise RuntimeError("close failed")

    with pytest.raises(RuntimeError, match="close failed") as error:
        with Library(_create_snapshot(tmp_path)) as library:
            library._session.start_pdf_cache()
            assert library._session.pdf_documents is not None
            library._session.pdf_documents.documents.update(
                {Path("test"): (Document(), (0, 0, 0, 0, 0, 0))}
            )
            raise ValueError("body failed")
    assert isinstance(error.value.__context__, ValueError)
    assert not library._session.is_open


def test_reopen_replaces_catalog_and_read_caches_after_close_error(tmp_path: Path) -> None:
    from goodnotes_cli.library import Library

    class Document:
        def close(self):
            raise RuntimeError("close failed")

    library = Library(_create_snapshot(tmp_path))
    with pytest.raises(RuntimeError, match="close failed"):
        with library:
            assert library.pages("doc-hostile")
            old_pages = library._pages
            old_catalog = library._catalog
            library._session.start_pdf_cache()
            assert library._session.pdf_documents is not None
            library._session.pdf_documents.documents.update(
                {Path("test"): (Document(), (0, 0, 0, 0, 0, 0))}
            )
    assert library._pages is not old_pages
    assert library._catalog is not old_catalog
    with library:
        assert library.pages("doc-hostile")
        assert library._pages is not old_pages
