"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from ..config import Settings, default_data_dir
from ..errors import (
    FilesystemAccessError,
    GoodnotesError,
)
from ..native import NativeError
from ..presentation.json import emit
from ..presentation.selection import select_pages
from ..storage.paths import _remove_entry_at, stat_key

if TYPE_CHECKING:
    from ..library import Library
    from ..models import Page
    from ..storage.artifacts import ArtifactCache

from .core import ItemSelector, command, repository, repository_query, settings


@command("export")
def export_notebook(
    ctx: typer.Context,
    notebook: ItemSelector,
    output: Annotated[Path, typer.Option(help="New PDF file to write.")],
    layer: Annotated[str, typer.Option(help="native, background, or annotated.")] = "native",
    pages: Annotated[
        str | None,
        typer.Option(help="Selected pages in listed order, e.g. 1-3,7. Defaults to all."),
    ] = None,
) -> None:
    """Export all pages, or selected pages in listed order, to a new PDF.

    The default native layer reconstructs copied ink and requires the native runtime.
    Check coverage and warnings. Explicit background output omits native ink. Annotated requires
    attach-pdf and uses an unverified user export. Existing outputs are never replaced.

    Example: goodnotes export "Course Notes" --output /tmp/course.pdf
    """

    with repository(ctx) as lib:
        from ..rendering import export_pdf

        all_pages = repository_query(ctx, lib, lib.pages, notebook)
        selected = (
            select_pages(pages, len(all_pages), maximum=len(all_pages))
            if pages is not None
            else None
        )
        emit(
            export_pdf(lib.sandbox, all_pages, output, layer=layer, page_numbers=selected),
            settings(ctx),
        )


@command(hidden=True)
def render(
    ctx: typer.Context,
    notebook: ItemSelector,
    output: Annotated[
        Path | None, typer.Option(help="Save a persistent PNG outside the cache.")
    ] = None,
    page: Annotated[int, typer.Option(min=1)] = 1,
    layer: str = "native",
    dpi: Annotated[int, typer.Option(min=72, max=300, hidden=True)] = 150,
) -> None:
    """Render one page to a temporary cached PNG, or save it with --output.

    Defaults to native reconstruction with coverage. Explicit background output
    omits native ink; annotated requires attach-pdf. Page numbers start at 1.
    Cached images expire after a day of inactivity and share a 100 MB limit.
    Returned paths are protected for five minutes. Explicit outputs are never replaced.

    Example: goodnotes render "Course Notes" --page 3 --output /tmp/page-3.png
    """

    with repository(ctx) as lib:
        from ..rendering import cached_render_pages, render_page

        all_pages = repository_query(ctx, lib, lib.pages, notebook)
        number = select_pages(str(page), len(all_pages))[0]
        if output is None:
            from ..storage.artifacts import ArtifactCache

            with ArtifactCache(_artifact_directory(settings(ctx)), lib.sandbox) as cache:
                result = cached_render_pages(
                    lib.sandbox, [all_pages[number - 1]], cache, layer=layer, dpi=dpi
                )[0]
                cache.renew_leases()
                emit(result, settings(ctx))
            return
        result = render_page(lib.sandbox, all_pages[number - 1], output, layer=layer, dpi=dpi)
        emit(result, settings(ctx))


@command("attach-pdf", hidden=True)
def attach_pdf(
    ctx: typer.Context,
    notebook: ItemSelector,
    pdf: Annotated[Path, typer.Argument(help="Existing PDF exported from Goodnotes.")],
) -> None:
    """Register a Goodnotes-exported PDF for annotated rendering and export.

    Checks file integrity and page count, not annotation inclusion or page order.
    Keep the PDF unchanged at its path. Re-register after a new snapshot generation.
    Does not add the PDF's text to read, search, or scan-pdfs.

    Example: goodnotes attach-pdf "Course Notes" /path/to/annotated.pdf
    """

    with repository(ctx) as lib:
        from ..rendering import register_annotated_pdf

        item = repository_query(ctx, lib, lib.resolve, notebook, kind="notebook")
        page_count = len(repository_query(ctx, lib, lib.pages, notebook))
        emit(
            register_annotated_pdf(lib.sandbox, item.id, pdf, expected_pages=page_count),
            settings(ctx),
        )


@command()
def bundle(
    ctx: typer.Context,
    notebook: ItemSelector,
    output: Annotated[
        Path | None, typer.Option(help="Save a persistent bundle outside the cache.")
    ] = None,
    pages: str = "1",
    layer: str = "native",
    dpi: Annotated[int, typer.Option(min=72, max=300, hidden=True)] = 150,
    max_chars: Annotated[int, typer.Option(min=1, max=1000000)] = 12000,
    source: Annotated[
        str, typer.Option(help="text (PDF + typed text), pdf, textbox, handwriting, or all.")
    ] = "text",
) -> None:
    """Inspect pages using a temporary cached bundle, or save it with --output.

    Select up to 10 distinct pages with --pages, e.g. 1-3,7. One character budget
    covers all text. Defaults to native ink images, PDF and typed text. Open each image
    before explaining notes or diagrams. Recognition metadata is not a filter for visible ink.
    Native reconstruction
    reports coverage; annotated requires attach-pdf. Text keeps its original source labels.
    --source filters returned text only; the image still uses the selected layer.
    Returns page evidence directly. A bundle with at least one rendered native image keeps
    text for failed pages and reports their render errors; a bundle with no rendered images
    remains an error. Global --verbose includes full metadata.
    Cached outputs share a 100 MB limit and expire after a day of inactivity.
    Returned paths are protected for five minutes; explicit outputs remain until removed.

    Example: goodnotes bundle "Course Notes" --pages 1-3 --output /tmp/course-pages
    """

    from ..rendering.bundle import (
        _bundle_content,
        _bundle_response,
        _read_bundle_text,
        _write_bundle_manifest,
    )
    from ..rendering.publication import publish_bundle

    if source not in {"text", "all", "pdf", "handwriting", "textbox"}:
        raise GoodnotesError("--source must be text, all, pdf, handwriting, or textbox.")
    cfg = settings(ctx)
    with repository(ctx) as lib:
        from ..rendering import render_pages

        sandbox = lib.sandbox
        item = repository_query(ctx, lib, lib.resolve, notebook, kind="notebook")
        all_pages = repository_query(ctx, lib, lib.pages, item)
        selected = select_pages(pages, len(all_pages), maximum=10)
        if not item.id or any(all_pages[number - 1].document_id != item.id for number in selected):
            raise GoodnotesError("Bundle pages have missing or inconsistent document identity.")
        if output is None:
            from ..storage.artifacts import ArtifactCache

            try:
                with ArtifactCache(_artifact_directory(cfg), sandbox) as cache:
                    response = _cached_bundle(
                        ctx,
                        lib,
                        item.id,
                        all_pages,
                        selected,
                        cache,
                        layer,
                        dpi,
                        max_chars,
                        source,
                    )
                    if cfg.fields and "document_id" in {
                        field.strip() for field in cfg.fields.split(",")
                    }:
                        response["document_id"] = item.id
                    cache.renew_leases()
                    emit(response, cfg)
            except NativeError as exc:
                _bundle_native_remedy(exc)
                raise
            return
        destination = sandbox.ensure_output(output)
        parent_existed = destination.parent.exists()
        temporary: Path | None = None
        temporary_identity: tuple[int, int] | None = None
        parent_identity: tuple[int, int] | None = None
        parent_fd = -1
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            parent_fd = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            parent_identity = stat_key(os.fstat(parent_fd))[:2]
            temporary = Path(tempfile.mkdtemp(prefix=".goodnotes-bundle-", dir=destination.parent))
            temporary_identity = stat_key(
                os.stat(temporary.name, dir_fd=parent_fd, follow_symlinks=False)
            )[:2]
            if stat_key(destination.parent.stat())[:2] != parent_identity:
                raise GoodnotesError("The bundle output parent changed during staging.")
            filenames = [f"page-{number:04d}.png" for number in selected]
            rendered_pages = render_pages(
                sandbox,
                [all_pages[number - 1] for number in selected],
                [temporary / filename for filename in filenames],
                layer=layer,
                dpi=dpi,
                allow_partial_native_errors=layer == "native",
            )
            texts = _read_bundle_text(
                lambda number, remaining: repository_query(
                    ctx, lib, lib.read_page, item.id, number, source=source, max_chars=remaining
                ),
                [all_pages[number - 1] for number in selected],
                max_chars,
            )
            manifest, truncated = _bundle_content(
                texts, item.id, selected, rendered_pages, max_chars
            )
            for entry in manifest:
                render = entry.get("render")
                image = entry.get("image")
                if isinstance(render, dict) and isinstance(image, str):
                    render["path"] = str(destination / image)
            _write_bundle_manifest(
                temporary, manifest, truncated, cfg.snapshot_id, sandbox.captured_at, source
            )
            publish_bundle(temporary, destination, expected_source_identity=temporary_identity)
        except BaseException as exc:
            if temporary is not None and temporary_identity is not None:
                try:
                    _remove_entry_at(
                        parent_fd, temporary.name, temporary, expected_identity=temporary_identity
                    )
                except (GoodnotesError, OSError):
                    pass
            if not parent_existed and parent_identity is not None:
                try:
                    if stat_key(destination.parent.lstat())[:2] == parent_identity:
                        destination.parent.rmdir()
                except OSError:
                    pass
            if isinstance(exc, NativeError):
                _bundle_native_remedy(exc)
            if isinstance(exc, OSError):
                raise FilesystemAccessError.from_exception(
                    exc, operation="write_bundle_output", path=destination, kind="output"
                ) from exc
            raise
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
        response = _bundle_response(destination, manifest, selected, layer, truncated, source)
        if cfg.fields and "document_id" in {field.strip() for field in cfg.fields.split(",")}:
            response["document_id"] = item.id
        emit(response, cfg)


def _artifact_directory(cfg: Settings) -> Path:
    return Path(cfg.cache_dir or cfg.data_dir or default_data_dir()).expanduser() / "artifacts"


def _bundle_native_remedy(exc: NativeError) -> None:
    exc.details["remedy"] = (
        f"{exc.details['remedy']} For page backgrounds without annotations, "
        "rerun bundle with --layer background."
    )


def _cached_bundle(
    ctx: typer.Context,
    lib: Library,
    notebook: str,
    all_pages: list[Page],
    selected: list[int],
    cache: ArtifactCache,
    layer: str,
    dpi: int,
    max_chars: int,
    source: str,
) -> dict[str, Any]:
    from ..rendering import cached_render_pages
    from ..rendering.bundle import (
        _bundle_content,
        _bundle_response,
        _read_bundle_text,
        _write_bundle_manifest,
    )
    from ..storage.artifacts import artifact_key

    cfg = settings(ctx)
    pages = [all_pages[number - 1] for number in selected]
    texts = _read_bundle_text(
        lambda number, remaining: repository_query(
            ctx, lib, lib.read_page, notebook, number, source=source, max_chars=remaining
        ),
        pages,
        max_chars,
    )
    rendered = cached_render_pages(
        lib.sandbox,
        pages,
        cache,
        layer=layer,
        dpi=dpi,
        allow_partial_native_errors=layer == "native",
    )
    # Text always comes from this request's pinned read view, including on image hits.
    manifest, truncated = _bundle_content(texts, notebook, selected, rendered, max_chars)
    key = artifact_key(
        {
            "bundle": 1,
            "snapshot": cfg.snapshot_id,
            "captured_at": lib.sandbox.captured_at,
            "pages": manifest,
            "source": source,
        }
    )
    destination = cache.destination(key)
    for entry in manifest:
        render = entry.get("render")
        image = entry.get("image")
        if isinstance(render, dict) and isinstance(image, str):
            render["path"] = str(destination / image)
    response = _bundle_response(destination, manifest, selected, layer, truncated, source)
    response["temporary"] = True
    if cache.lookup(key) is not None:
        return response
    with cache.staging() as temporary:
        for entry, image in zip(manifest, rendered, strict=True):
            image_name = entry.get("image")
            image_path = image.get("path")
            if isinstance(image_name, str) and isinstance(image_path, str):
                os.link(Path(image_path), temporary / image_name, follow_symlinks=False)
        _write_bundle_manifest(
            temporary, manifest, truncated, cfg.snapshot_id, lib.sandbox.captured_at, source
        )
        cache.publish(key, temporary, {"kind": "bundle"})
    return response
