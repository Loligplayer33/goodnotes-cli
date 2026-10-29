"""Managed page render caching and publication."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import GoodnotesError
from ..models import Page
from ..storage.paths import stat_key
from .backgrounds import (
    _check_dpi,
    _pdf_source_signature,
    _PdfDocumentCache,
    _resolve_attachment_path,
)
from .pages import (
    _check_layer,
    _close_registered_pdfs,
    _is_page_local_native_error,
    _native_record_reader,
    _native_render_error,
    _render_page_with_resources,
)
from .registered import (
    _check_annotated_page_number,
    _check_registered_page_count,
    _open_registered_pdf,
    _registered_page_count,
    _RegisteredPdf,
)
from .types import SandboxProtocol

if TYPE_CHECKING:
    from ..native import NativeError
    from ..storage.artifacts import ArtifactCache
_RENDER_CACHE_VERSION = 8


def cached_render_pages(
    sandbox: SandboxProtocol,
    pages: list[Page],
    cache: ArtifactCache,
    *,
    layer: str = "background",
    dpi: int = 150,
    allow_partial_native_errors: bool = False,
) -> list[dict[str, object]]:
    """Reuse verified page renders without opening a native engine on cache hits.

    Keys belong to one immutable read view and include the current input metadata.
    PNG publication also deduplicates identical images across views. Registered PDFs
    retain their full integrity check once per operation, even on an artifact hit. When
    ``allow_partial_native_errors=True``, failed page renders are retried on the next call and
    are never published as page-image cache entries.
    """
    from ..native import NativeError, native_store_fingerprint
    from ..native.assets import native_attachment_fingerprint
    from ..presentation.json import serializable
    from ..storage.artifacts import artifact_key

    _check_layer(layer)
    _check_dpi(dpi)
    if not pages:
        raise GoodnotesError("Provide at least one page to render.")
    native_inputs = native_store_fingerprint(sandbox) if layer == "native" else None
    native_attachments: dict[str, object] = {}
    attachments: dict[str, Path] = {}
    signatures: dict[Path, tuple[int, int, int, int, int, int]] = {}
    registered: dict[str, _RegisteredPdf] = {}
    rendered: list[dict[str, object]] = []
    first_page_error: NativeError | None = None

    def background_identity(page: Page) -> object:
        if layer == "annotated":
            if page.document_id not in registered:
                verified = _open_registered_pdf(sandbox, page.document_id)
                try:
                    _check_registered_page_count(sandbox, page.document_id, verified.entry)
                except BaseException:
                    verified.close()
                    raise
                registered[page.document_id] = verified
            verified = registered[page.document_id]
            verified.ensure_unchanged()
            _check_annotated_page_number(page, _registered_page_count(verified.entry))
            return verified.entry, verified.signature
        attachment = page.attachment_path or ""
        if attachment not in attachments:
            attachments[attachment] = _resolve_attachment_path(sandbox, page)
        path = attachments[attachment]
        if path not in signatures:
            signatures[path] = _pdf_source_signature(path)
        return str(path), signatures[path]

    def check_inputs() -> None:
        for path, expected in signatures.items():
            if _pdf_source_signature(path) != expected:
                raise GoodnotesError("A copied page background changed during rendering.")
        if native_inputs is not None and native_store_fingerprint(sandbox) != native_inputs:
            raise GoodnotesError("The copied native store changed during rendering.")
        for document_id, expected in native_attachments.items():
            if native_attachment_fingerprint(sandbox, document_id) != expected:
                raise GoodnotesError("Copied native image attachments changed during rendering.")
        for verified in registered.values():
            verified.ensure_unchanged()

    try:
        with ExitStack() as resources:
            pdf_cache = resources.enter_context(_PdfDocumentCache())
            record_reader: Callable[[str], list[tuple[bytes, bytes]]] | None = None
            pending: list[tuple[int, str, Path, dict[str, object]]] = []

            def render_staged_page(page: Page) -> tuple[Path, dict[str, object]]:
                stage_resources = ExitStack()
                try:
                    stage = stage_resources.enter_context(cache.staging())
                    result = _render_page_with_resources(
                        sandbox,
                        page,
                        sandbox.ensure_output(stage / "page.png"),
                        layer=layer,
                        dpi=dpi,
                        pdf_cache=pdf_cache,
                        registered=registered,
                        record_reader=record_reader,
                    )
                except BaseException:
                    stage_resources.close()
                    raise
                resources.callback(stage_resources.close)
                return stage, result

            snapshot = (
                str(sandbox.root),
                stat_key(sandbox.root.lstat()),
                stat_key((sandbox.root / "goodnotes-snapshot.json").lstat()),
            )
            native_key = artifact_key(native_inputs) if native_inputs is not None else None
            for page in pages:
                if layer == "native" and page.document_id not in native_attachments:
                    native_attachments[page.document_id] = native_attachment_fingerprint(
                        sandbox, page.document_id
                    )
                background = background_identity(page)
                key = artifact_key(
                    {
                        "renderer": _RENDER_CACHE_VERSION,
                        "snapshot": snapshot,
                        "page": serializable(page),
                        "background": background,
                        "native": native_key,
                        "native_attachments": native_attachments.get(page.document_id),
                        "layer": layer,
                        "dpi": dpi,
                    }
                )
                hit = cache.lookup(key)
                if hit is not None:
                    result = dict(hit.result)
                    result["path"] = str(hit.directory / "page.png")
                else:
                    if layer == "native" and record_reader is None:
                        record_reader = resources.enter_context(_native_record_reader(sandbox))
                    try:
                        stage, result = render_staged_page(page)
                    except NativeError as exc:
                        if not allow_partial_native_errors or not _is_page_local_native_error(exc):
                            raise
                        first_page_error = first_page_error or exc
                        rendered.append(_native_render_error(page, exc))
                        continue
                    result["path"] = str(cache.destination(key) / "page.png")
                    pending.append((len(rendered), key, stage, result))
                if result.get("document_id") != page.document_id:
                    raise GoodnotesError(
                        "Bundle page render has missing or inconsistent document identity."
                    )
                if result.get("page") != page.number:
                    raise GoodnotesError(
                        "Bundle page render has missing or inconsistent page identity."
                    )
                result["captured_at"] = getattr(sandbox, "captured_at", None)
                result["temporary"] = True
                rendered.append(result)
            check_inputs()
            if first_page_error is not None and not any(
                "render_error" not in result for result in rendered
            ):
                raise first_page_error
            for index, key, stage, result in pending:
                cache.publish(key, stage, result)
                rendered[index] = result
            check_inputs()
    finally:
        _close_registered_pdfs(registered)
    return rendered
