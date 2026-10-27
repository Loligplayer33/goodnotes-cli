"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

from typing import Annotated

import typer

from ..errors import (
    GoodnotesError,
    TextIndexUnavailableError,
)
from ..presentation.json import emit, emit_error, serializable
from ..presentation.selection import bound_page_text, select_pages, snippet_hits, window
from .core import (
    FolderOption,
    ItemSelector,
    SearchFolderOption,
    SearchNotebookOption,
    command,
    repository,
    repository_query,
    settings,
)


@command("ls")
def list_items(
    ctx: typer.Context,
    folder: FolderOption = "/",
    kind: Annotated[
        str | None, typer.Option(help="Filter to folder or notebook before paging.")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List a folder's direct children.

    --folder accepts a path, UUID, or unique name; / selects the root. Continue with
    next_offset and pin the returned snapshot ID using global --expect-snapshot.

    Example: goodnotes ls --folder "Course" --limit 20
    """
    if kind not in {None, "folder", "notebook"}:
        raise GoodnotesError("--kind must be folder or notebook.")
    with repository(ctx) as lib:
        items = repository_query(ctx, lib, lib.list_items, folder)
        if kind is not None:
            items = [item for item in items if item.kind == kind]
        emit(window(items, limit, offset), settings(ctx))


@command(hidden=True)
def tree(
    ctx: typer.Context,
    folder: FolderOption = "/",
    depth: Annotated[int, typer.Option(min=1, max=50)] = 3,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List descendants within a depth and result limit.

    --folder selects the starting folder, / by default. Continue with next_offset;
    global --expect-snapshot keeps pagination on one generation.

    Example: goodnotes tree --folder "Course" --depth 2
    """
    with repository(ctx) as lib:
        items = repository_query(ctx, lib, lib.tree, folder, max_depth=depth)
        emit(window(items, limit, offset), settings(ctx))


@command()
def info(ctx: typer.Context, notebook: ItemSelector) -> None:
    """Show a folder or notebook's metadata.

    NOTEBOOK accepts an exact UUID, full library path, or unique exact name.
    Ambiguous names fail; use a full path or UUID to disambiguate.

    Example: goodnotes info "Course Notes"
    """
    with repository(ctx) as lib:
        emit(repository_query(ctx, lib, lib.resolve, notebook), settings(ctx))


@command()
def pages(
    ctx: typer.Context,
    notebook: ItemSelector,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    with_recognition: Annotated[
        bool,
        typer.Option(hidden=True, help="Recognition metadata is always included."),
    ] = False,
) -> None:
    """List pages in current notebook order, numbered from 1.

    Returns pages, next_offset, and recognition metadata without candidate text.
    Recognition can prioritize image inspection; it cannot exclude diagrams or unindexed ink.
    annotation_count counts projected note items, not ink; null means unavailable.
    Continue with --offset and pin --expect-snapshot for a consistent generation.

    Example: goodnotes pages "Course Notes" --limit 20
    """
    with repository(ctx) as lib:
        result = window(repository_query(ctx, lib, lib.pages, notebook), limit, offset)
        result["pages"] = result.pop("items")
        recognition = repository_query(ctx, lib, lib.recognition_status, notebook)
        result["pages"] = [
            {**serializable(page), "recognition": serializable(recognition[page.id])}
            for page in result["pages"]
        ]
        result["annotation_count_source"] = (
            "projection.notes_items"
            if any(page["annotation_count"] is not None for page in result["pages"])
            else None
        )
        emit(result, settings(ctx))


@command("read")
def read_text(
    ctx: typer.Context,
    notebook: ItemSelector,
    page: Annotated[int | None, typer.Option(min=1, hidden=True)] = None,
    pages: Annotated[str | None, typer.Option(help="Up to 20 pages, e.g. 1-3,7.")] = None,
    source: Annotated[
        str, typer.Option(help="text (PDF + typed text), pdf, textbox, handwriting, or all.")
    ] = "text",
    max_chars: Annotated[int, typer.Option(min=1, max=1000000)] = 12000,
) -> None:
    """Read a rough text overview. Use bundle images for explanation and visual study.

    Defaults to page 1. Choose --pages, up to 20 distinct pages. The
    character budget covers all selected pages; check truncated in the response.
    Defaults to PDF and typed text. --source handwriting or all opts into unverified stored
    recognition candidates. Text cannot establish the contents of diagrams or native ink.

    Example: goodnotes read "Course Notes" --pages 1-3,7 --source handwriting
    """
    if page is not None and pages is not None:
        raise GoodnotesError("Use --page or --pages, not both.")
    if source not in {"text", "all", "pdf", "handwriting", "textbox"}:
        raise GoodnotesError("--source must be text, all, pdf, handwriting, or textbox.")
    with repository(ctx) as lib:
        all_pages = repository_query(ctx, lib, lib.pages, notebook)
        selected = select_pages(pages or str(page or 1), len(all_pages))
        results = []
        remaining = max_chars
        for number in selected:
            page_data = repository_query(
                ctx,
                lib,
                lib.read_page,
                notebook,
                number,
                source=source,
                max_chars=remaining,
            )
            data = serializable(page_data)
            remaining -= sum(len(part["text"]) for part in data["parts"])
            results.append(data)
        truncated = bound_page_text(results, max_chars)
        emit({"pages": results, "truncated": truncated}, settings(ctx))


@command()
def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Literal, case-insensitive text to find.")],
    folder: SearchFolderOption = None,
    notebook: SearchNotebookOption = None,
    source: Annotated[
        str, typer.Option(help="text (PDF + typed text), pdf, textbox, handwriting, or all.")
    ] = "text",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Search stored text for a literal, case-insensitive phrase.

    Choose --notebook for one notebook or --folder for a subtree. coverage_detail
    reports scoped source availability, not proof that every page was indexed.
    Defaults to PDF and typed text. Use --source all to include stored handwriting candidates.
    No hits do not prove absence; scan-pdfs searches PDF backgrounds separately.
    Snippets are capped at 320 characters.

    Example: goodnotes search "regression" --folder "Course" --limit 10
    """
    selected_folder = _search_folder(folder, notebook)
    with repository(ctx) as lib:
        try:
            hits = repository_query(
                ctx,
                lib,
                lib.search,
                query,
                folder=selected_folder,
                notebook=notebook,
                source=source,
                limit=offset + limit + 1,
            )
        except TextIndexUnavailableError as exc:
            coverage = repository_query(
                ctx,
                lib,
                lib.search_coverage,
                folder=selected_folder,
                notebook=notebook,
                source=source,
            )
            warnings = coverage.pop("warnings", [])
            emit_error(
                "operation_failed",
                str(exc),
                details={
                    "coverage": "stored-index",
                    "coverage_detail": coverage,
                    "warnings": warnings,
                },
            )
            raise SystemExit(1) from None
        selected = snippet_hits(hits[offset : offset + limit], query)
        coverage = repository_query(
            ctx,
            lib,
            lib.search_coverage,
            folder=selected_folder,
            notebook=notebook,
            source=source,
        )
        warnings = coverage.pop("warnings")
        emit(
            {
                "hits": selected,
                "next_offset": offset + limit if len(hits) > offset + limit else None,
                "coverage": "stored-index",
                "coverage_detail": coverage,
                "warnings": warnings,
            },
            settings(ctx),
        )


@command("scan-pdfs", hidden=True)
def scan_pdfs(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Literal, case-insensitive PDF text to find.")],
    folder: SearchFolderOption = None,
    notebook: SearchNotebookOption = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    page_limit: Annotated[int, typer.Option(min=1, max=250)] = 200,
    page_offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Search PDF background text when the stored index misses it.

    Choose --notebook or --folder. Continue next_page_offset with --page-offset,
    the same scope, query, and snapshot. coverage_complete requires a complete
    scan from offset 0 without extraction gaps. Does not search handwriting or attached PDFs.

    Example: goodnotes scan-pdfs "regression" --folder "Course" --page-limit 50
    """
    selected_folder = _search_folder(folder, notebook)
    with repository(ctx) as lib:
        result = repository_query(
            ctx,
            lib,
            lib.scan_pdfs,
            query,
            folder=selected_folder,
            notebook=notebook,
            limit=limit,
            page_limit=page_limit,
            page_offset=page_offset,
        )
        result["hits"] = snippet_hits(result["hits"], query)
        emit(result, settings(ctx))


def _search_folder(folder: str | None, notebook: str | None) -> str:
    if folder is not None and notebook is not None:
        raise GoodnotesError("Use --folder or --notebook, not both.")
    return folder if folder is not None else "/"
