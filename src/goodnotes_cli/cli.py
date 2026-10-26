"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, TypedDict, cast

import typer
from typer.core import TyperGroup

from . import __version__, build_info
from .cache import read_view, refresh_cache
from .config import Settings, default_data_dir, default_library
from .errors import (
    CacheAccessError,
    FilesystemAccessError,
    GoodnotesError,
    TextIndexUnavailableError,
)
from .native import NativeError
from .output import (
    Projection,
    bound_page_text,
    emit,
    emit_error,
    output_contract,
    select_pages,
    serializable,
    snippet_hits,
    validate_projection,
    window,
)
from .snapshot import create_snapshot
from .watch import watch_library

if TYPE_CHECKING:
    from .artifacts import ArtifactCache
    from .library import Library
    from .models import Page

ItemSelector = Annotated[
    str, typer.Argument(help="Exact UUID, full library path, or unique exact name.")
]
FolderOption = Annotated[
    str, typer.Option(help="Folder UUID, path, or unique exact name; / is the root.")
]
SearchFolderOption = Annotated[
    str | None, typer.Option(help="Folder selector; defaults to /. Use with --notebook omitted.")
]
SearchNotebookOption = Annotated[
    str | None, typer.Option(help="Exact notebook UUID, path, or unique name; excludes --folder.")
]


class AgentGroup(TyperGroup):
    """Keep expected errors machine-readable, including command parsing errors."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().main(*args, **kwargs)
        except GoodnotesError as exc:
            emit_error(
                getattr(exc, "code", "operation_failed"),
                str(exc),
                details=getattr(exc, "details", None),
            )
            raise SystemExit(1) from None


class _BundlePage(TypedDict):
    page: int
    image: str | None
    text: dict[str, Any]
    render: NotRequired[dict[str, Any]]
    render_error: NotRequired[dict[str, Any]]
    warnings: NotRequired[list[str]]


app = typer.Typer(
    cls=AgentGroup,
    help=(
        "Browse, read, search, and export a copied Goodnotes library. Compact JSON by default.\n\n"
        "Start with sync, then bundle for visual study or read for a rough text overview. "
        "Open bundle images before explaining notes, diagrams, or handwriting. "
        "Put global options before the command. schema lists advanced commands; "
        "schema COMMAND gives full options and output details."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def command(*args: Any, **kwargs: Any) -> Any:
    def decorate(function: Any) -> Any:
        command_name = (
            args[0]
            if args and isinstance(args[0], str)
            else kwargs.get("name") or function.__name__.replace("_", "-")
        )

        @wraps(function)
        def checked(*fargs: Any, **fkwargs: Any) -> Any:
            ctx = cast(typer.Context | None, fkwargs.get("ctx") or (fargs[0] if fargs else None))
            if ctx is not None and hasattr(ctx, "params") and isinstance(ctx.obj, Settings):
                cfg = settings(ctx)
                projection = Projection(
                    command_name,
                    frozenset(
                        name
                        for name, value in ctx.params.items()
                        if value is True or (name == "notebook" and value is not None)
                    ),
                )
                cfg.projection = projection
                validate_projection(cfg.fields, projection)
            try:
                return function(*fargs, **fkwargs)
            except BrokenPipeError:
                # Preserve the process-level broken-pipe contract in main().
                raise
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise GoodnotesError(str(exc)) from exc

        return app.command(*args, **kwargs)(checked)

    return decorate


def version(value: bool) -> None:
    if value:
        typer.echo(f"{__version__} (source {build_info()['source_sha256'][:12]})")
        raise typer.Exit()


@app.callback()
def configure(
    ctx: typer.Context,
    sandbox: Annotated[
        Path | None, typer.Option(envvar="GOODNOTES_SANDBOX", help="Use an existing read view.")
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            envvar="GOODNOTES_DATA_DIR",
            help="Managed cache directory; use the same selection for sync and reads.",
        ),
    ] = None,
    library_root: Annotated[
        str | None,
        typer.Option(
            hidden=True,
            envvar="GOODNOTES_LIBRARY_ROOT",
            help="Library root ID; all restores the combined view. Overrides cache selection.",
        ),
    ] = None,
    text: Annotated[bool, typer.Option("--text", help="Indent JSON for inspection.")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", hidden=True, help="Compact JSON, the default.")
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Include full metadata and native format diagnostics."),
    ] = False,
    fields: Annotated[
        str | None,
        typer.Option(
            help="Comma-separated row fields or response keys; see schema COMMAND output."
        ),
    ] = None,
    expect_snapshot: Annotated[
        str | None, typer.Option(help="Reject a changed pagination generation.")
    ] = None,
    show_version: Annotated[
        bool, typer.Option("--version", callback=version, is_eager=True, help="Print the version.")
    ] = False,
) -> None:
    ctx.obj = Settings(
        sandbox,
        data_dir,
        text and not json_output,
        fields,
        expect_snapshot,
        brief=not verbose and fields is None,
        library_root=library_root,
    )


def settings(ctx: typer.Context) -> Settings:
    return ctx.obj


@contextmanager
def repository(ctx: typer.Context) -> Generator[Library, None, None]:
    cfg = settings(ctx)
    with read_view(cfg) as sandbox:
        cfg.snapshot_id = sandbox.root.name
        with ExitStack() as stack:
            try:
                from .library import Library

                lib = stack.enter_context(Library(sandbox, root_folder_id=cfg.load_library_root()))
            except sqlite3.Error as exc:
                raise CacheAccessError(
                    "cache_invalid",
                    f"Could not read the copied library catalog: {sandbox.projection_path}",
                    operation="read_snapshot_catalog",
                    path=sandbox.projection_path,
                    cache_dir=cfg.cache_dir,
                ) from exc
            yield lib


def repository_query(
    ctx: typer.Context, lib: Library, query: Any, *args: Any, **kwargs: Any
) -> Any:
    """Run a snapshot query and retain cache details for SQLite read failures."""
    try:
        return query(*args, **kwargs)
    except sqlite3.Error as exc:
        cfg = settings(ctx)
        raise CacheAccessError(
            "cache_invalid",
            f"Could not query the copied library snapshot: {lib.sandbox.root}",
            operation="query_snapshot_library",
            path=lib.sandbox.root,
            cache_dir=cfg.cache_dir,
        ) from exc


@command()
def sync(
    ctx: typer.Context,
    library: Annotated[Path | None, typer.Option(help="Goodnotes Library directory.")] = None,
    force: Annotated[
        bool, typer.Option(help="Rebuild and verify even when files appear unchanged.")
    ] = False,
) -> None:
    """Create or refresh the managed library copy.

    Reads source files without opening source SQLite or writing to Goodnotes. Reuses
    an unchanged copy unless --force is set. Later source edits require another sync.
    Requires cache write access and initializes persistent locks for read-only readers.

    Example: goodnotes sync --library /path/to/Library
    """

    cfg = settings(ctx)
    if cfg.sandbox is not None:
        raise GoodnotesError("sync uses --library as its source; omit the global --sandbox option.")
    emit(
        refresh_cache(
            library or default_library(), cfg.data_dir or default_data_dir(), force=force
        ),
        cfg,
    )


@command(hidden=True)
def watch(
    ctx: typer.Context,
    library: Annotated[Path | None, typer.Option(help="Goodnotes Library directory.")] = None,
    interval: Annotated[
        float, typer.Option(min=0.05, max=3600, help="Seconds between metadata checks.")
    ] = 2,
    debounce: Annotated[
        float, typer.Option(min=0, max=60, help="Seconds to coalesce edits before capture.")
    ] = 1,
    iterations: Annotated[
        int | None, typer.Option(min=1, help="Stop after this many polling iterations.")
    ] = None,
) -> None:
    """Keep the copied library refreshed while this foreground process runs.

    Emits one JSON object per ready, updated, recovery, or error event. Unchanged
    checks stay quiet. Ctrl-C stops the watcher. This observes files on the Mac;
    it cannot establish whether the latest iPad changes have reached this Mac.

    Example: goodnotes watch --library /path/to/Library --interval 2
    """
    cfg = settings(ctx)
    if cfg.sandbox is not None:
        raise GoodnotesError(
            "watch uses --library as its source; omit the global --sandbox option."
        )
    failed = False
    for event in watch_library(
        library or default_library(),
        cfg.data_dir or default_data_dir(),
        interval=interval,
        debounce=debounce,
        iterations=iterations,
    ):
        emit(event, cfg)
        failed = event.get("event") == "error"
    if failed:
        # The error is already present in the JSONL stream. A bounded watcher
        # must also let process supervisors detect an unrecovered failure.
        raise SystemExit(1)


@command(hidden=True)
def snapshot(
    ctx: typer.Context,
    library: Annotated[Path, typer.Argument(help="Source Library directory.")],
    destination: Annotated[Path, typer.Argument(help="New snapshot directory to create.")],
) -> None:
    """Create an independent copy at a new destination.

    LIBRARY must contain Databases/ and Attachments/. Does not change the active cache.
    Read the result with the global --sandbox option.

    Example: goodnotes snapshot /path/to/Library /path/to/saved-copy
    """

    result = create_snapshot(library, destination)
    emit({"snapshot": result, "status": "ready"}, settings(ctx))


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
        from .documents import export_pdf

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
        from .documents import cached_render_pages, render_page

        all_pages = repository_query(ctx, lib, lib.pages, notebook)
        number = select_pages(str(page), len(all_pages))[0]
        if output is None:
            from .artifacts import ArtifactCache

            with ArtifactCache(_artifact_directory(settings(ctx)), lib.sandbox.root) as cache:
                result = cached_render_pages(
                    lib.sandbox, [all_pages[number - 1]], cache, layer=layer, dpi=dpi
                )[0]
        else:
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
        from .documents import register_annotated_pdf

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

    if source not in {"text", "all", "pdf", "handwriting", "textbox"}:
        raise GoodnotesError("--source must be text, all, pdf, handwriting, or textbox.")
    cfg = settings(ctx)
    with repository(ctx) as lib:
        from .documents import render_pages

        sandbox = lib.sandbox
        all_pages = repository_query(ctx, lib, lib.pages, notebook)
        selected = select_pages(pages, len(all_pages), maximum=10)
        if output is None:
            from .artifacts import ArtifactCache

            try:
                with ArtifactCache(_artifact_directory(cfg), sandbox.root) as cache:
                    response = _cached_bundle(
                        ctx,
                        lib,
                        notebook,
                        all_pages,
                        selected,
                        cache,
                        layer,
                        dpi,
                        max_chars,
                        source,
                    )
            except NativeError as exc:
                _bundle_native_remedy(exc)
                raise
            emit(response, cfg)
            return
        destination = sandbox.ensure_output(output)
        parent_existed = destination.parent.exists()
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".goodnotes-bundle-", dir=destination.parent))
            filenames = [f"page-{number:04d}.png" for number in selected]
            rendered_pages = render_pages(
                sandbox,
                [all_pages[number - 1] for number in selected],
                [temporary / filename for filename in filenames],
                layer=layer,
                dpi=dpi,
                allow_partial_native_errors=layer == "native",
            )
            manifest, truncated = _bundle_content(
                ctx, lib, notebook, selected, rendered_pages, max_chars, source
            )
            for entry in manifest:
                render = entry.get("render")
                image = entry.get("image")
                if isinstance(render, dict) and isinstance(image, str):
                    render["path"] = str(destination / image)
            _write_bundle_manifest(temporary, manifest, truncated, cfg, sandbox.captured_at, source)
            publish_bundle(temporary, destination)
        except BaseException as exc:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            if not parent_existed:
                try:
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
        emit(_bundle_response(destination, manifest, selected, layer, truncated, source), cfg)


def _artifact_directory(cfg: Settings) -> Path:
    return Path(cfg.cache_dir or cfg.data_dir or default_data_dir()).expanduser() / "artifacts"


def _bundle_native_remedy(exc: NativeError) -> None:
    exc.details["remedy"] = (
        f"{exc.details['remedy']} For page backgrounds without annotations, "
        "rerun bundle with --layer background."
    )


def _bundle_content(
    ctx: typer.Context,
    lib: Library,
    notebook: str,
    selected: list[int],
    rendered_pages: list[dict[str, object]],
    max_chars: int,
    source: str,
) -> tuple[list[_BundlePage], bool]:
    manifest: list[_BundlePage] = []
    remaining = max_chars
    for number, rendered in zip(selected, rendered_pages, strict=True):
        page_data = repository_query(
            ctx, lib, lib.read_page, notebook, number, source=source, max_chars=remaining
        )
        text = serializable(page_data)
        remaining -= sum(len(part["text"]) for part in text["parts"])
        render_error = rendered.get("render_error")
        if isinstance(render_error, dict):
            manifest.append(
                {
                    "page": number,
                    "image": None,
                    "render_error": dict(render_error),
                    "warnings": [
                        f"Native image rendering failed for page {number}; no image is available."
                    ],
                    "text": text,
                }
            )
        else:
            manifest.append(
                {
                    "page": number,
                    "image": f"page-{number:04d}.png",
                    "render": dict(rendered),
                    "text": text,
                }
            )
    return manifest, bound_page_text([entry["text"] for entry in manifest], max_chars)


def _write_bundle_manifest(
    temporary: Path,
    manifest: list[_BundlePage],
    truncated: bool,
    cfg: Settings,
    captured_at: str | None,
    source: str,
) -> None:
    content = {
        "snapshot": cfg.snapshot_id,
        "captured_at": captured_at,
        "pages": manifest,
        "truncated": truncated,
        "text_source": source,
    }
    failed_pages = [
        entry["page"] for entry in manifest if isinstance(entry.get("render_error"), dict)
    ]
    if failed_pages:
        content.update(
            {
                "status": "partial",
                "failed_pages": failed_pages,
                "warnings": [_bundle_partial_warning(failed_pages)],
            }
        )
    (temporary / "text.json").write_text(json.dumps(content, ensure_ascii=False))


def _bundle_response(
    destination: Path,
    manifest: list[_BundlePage],
    selected: list[int],
    layer: str,
    truncated: bool,
    source: str,
) -> dict[str, Any]:
    response = {
        "directory": str(destination),
        "manifest": str(destination / "text.json"),
        "pages": selected,
        "layer": layer,
        "truncated": truncated,
        "captured_at": next(
            (
                entry["render"].get("captured_at")
                for entry in manifest
                if isinstance(entry.get("render"), dict)
            ),
            None,
        ),
        "text_source": source,
        "page_evidence": [
            {
                **entry,
                "image": (
                    str(destination / cast(str, entry["image"]))
                    if isinstance(entry.get("image"), str)
                    else None
                ),
            }
            for entry in manifest
        ],
    }
    failed_pages = [
        entry["page"] for entry in manifest if isinstance(entry.get("render_error"), dict)
    ]
    if failed_pages:
        response.update(
            {
                "status": "partial",
                "failed_pages": failed_pages,
                "warnings": [_bundle_partial_warning(failed_pages)],
            }
        )
    return response


def _bundle_partial_warning(failed_pages: list[int]) -> str:
    pages = ", ".join(str(page) for page in failed_pages)
    page_label = "page" if len(failed_pages) == 1 else "pages"
    image_notice = "no image was produced" if len(failed_pages) == 1 else "no images were produced"
    return (
        f"Native rendering failed for {page_label} {pages}; source-labelled text evidence "
        f"remains available, but {image_notice}."
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
    from .artifacts import artifact_key
    from .documents import cached_render_pages

    cfg = settings(ctx)
    rendered = cached_render_pages(
        lib.sandbox,
        [all_pages[number - 1] for number in selected],
        cache,
        layer=layer,
        dpi=dpi,
        allow_partial_native_errors=layer == "native",
    )
    # Text always comes from this request's pinned read view, including on image hits.
    manifest, truncated = _bundle_content(ctx, lib, notebook, selected, rendered, max_chars, source)
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
        _write_bundle_manifest(temporary, manifest, truncated, cfg, lib.sandbox.captured_at, source)
        cache.publish(key, temporary, {"kind": "bundle"})
    return response


def publish_bundle(temporary: Path, destination: Path) -> None:
    """Reserve the output directory and publish each file without replacing existing data."""
    destination.mkdir(mode=0o700)
    published: list[Path] = []
    try:
        # Publish the manifest last; it describes the completed bundle.
        files = sorted(temporary.iterdir(), key=lambda path: path.name == "text.json")
        for source in files:
            target = destination / source.name
            os.link(source, target)
            published.append(target)
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


@command()
def doctor(
    ctx: typer.Context,
    notebook: Annotated[
        str | None, typer.Option(help="Scope page/index diagnostics to one notebook.")
    ] = None,
    access_only: Annotated[
        bool,
        typer.Option(help="Check filesystem access without opening databases or writing files."),
    ] = False,
    library: Annotated[
        Path | None, typer.Option(help="Optional source Library to check with --access-only.")
    ] = None,
    scratch_dir: Annotated[
        Path | None, typer.Option(help="Native scratch directory to check with --access-only.")
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option(help="Output directory to check with --access-only.")
    ] = None,
) -> None:
    """Report local library counts, missing metadata, and text-source warnings.

    storage contains cache_dir, snapshot_path, and captured_at; unknown values are null.
    Reads the selected copy without refreshing it or contacting Goodnotes.

    Example: goodnotes doctor
    """
    cfg = settings(ctx)
    if access_only:
        if notebook is not None:
            raise GoodnotesError("--notebook cannot be combined with --access-only.")
        from .diagnostics import inspect_environment
        from .native import runtime_status

        result = inspect_environment(
            cfg.data_dir or default_data_dir(),
            library,
            sandbox=cfg.sandbox,
            scratch_dir=scratch_dir,
            output_dir=output_dir,
        )
        result["native_runtime"] = runtime_status()
        emit(result, cfg)
        return
    if any(value is not None for value in (library, scratch_dir, output_dir)):
        raise GoodnotesError("--library, --scratch-dir, and --output-dir require --access-only.")
    with repository(ctx) as lib:
        cfg = settings(ctx)
        result = repository_query(ctx, lib, lib.stats, notebook=notebook)
        result["storage"] = {
            "cache_dir": cfg.cache_dir,
            "snapshot_path": lib.sandbox.root,
            "captured_at": lib.sandbox.captured_at,
        }
        marker = lib.sandbox.marker_data
        validation = marker.get("projection_validation")
        result["provenance"] = {
            "cli": build_info(),
            "snapshot_format": marker.get("format_version"),
            "producer": marker.get("producer"),
            "source_app": marker.get("source_app"),
            "library_format": (
                validation.get("library_format") if isinstance(validation, dict) else None
            ),
        }
        from .native import runtime_status

        result["native_runtime"] = runtime_status()
        emit(result, cfg)


@command()
def schema(
    ctx: typer.Context,
    command_name: Annotated[
        str | None, typer.Argument(metavar="COMMAND", help="Command to describe in detail.")
    ] = None,
) -> None:
    """Show the command index or one command's machine-readable contract.

    Does not load notes or refresh the library. Targeted details include registered
    arguments, options, defaults, bounds, global options, and usage notes.

    Example: goodnotes schema read
    """
    if command_name is not None:
        root = ctx.find_root()
        group = root.command
        assert isinstance(group, TyperGroup)
        target = group.commands.get(command_name)
        if target is None:
            raise typer.BadParameter(f"Unknown command: {command_name}", param_hint="COMMAND")
        with typer.Context(target, parent=root, info_name=command_name) as target_ctx:
            parameters = target.get_params(target_ctx)
            result: dict[str, Any] = {
                "version": __version__,
                "build": build_info(),
                "command": command_name,
                "usage": target.get_usage(target_ctx).removeprefix("Usage: "),
                "help": target.help,
                "arguments": [
                    parameter_contract(p) for p in parameters if p.param_type_name == "argument"
                ],
                "options": [
                    parameter_contract(p) for p in parameters if p.param_type_name == "option"
                ],
                "global_options": [parameter_contract(p) for p in group.get_params(root)],
            }
            contract = output_contract(command_name)
            if contract is not None:
                result["output"] = contract
            emit(
                result,
                settings(ctx),
            )
        return
    group = ctx.find_root().command
    assert isinstance(group, TyperGroup)
    emit(
        {
            "version": __version__,
            "build": build_info(),
            "format": "compact-json",
            "page_numbers": "one-based",
            "commands": {
                name: target.get_short_help_str()
                for name, target in group.commands.items()
                if not target.hidden
            },
            "advanced_commands": {
                name: target.get_short_help_str()
                for name, target in group.commands.items()
                if target.hidden
            },
            "limits": {
                "read_pages": 20,
                "read_chars": 12000,
                "search_hits": 20,
                "snippet_chars": 320,
                "list_items": 100,
                "bundle_pages": 10,
            },
            "limits_note": (
                "read_pages, snippet_chars, bundle_pages are maxima; other limits are defaults. "
                "Use schema COMMAND for accepted option ranges."
            ),
            "global_options": [
                flag
                for parameter in group.get_params(ctx.find_root())
                if not getattr(parameter, "hidden", False)
                for flag in parameter.opts
            ],
            "usage_notes": [
                "Global options go before the command. schema COMMAND gives targeted details.",
                "A managed cache can select a default root in library-root.json "
                "with root_folder_id. --library-root ID or all overrides it; "
                "info exposes root_folder_id with --fields. Explicit --sandbox reads "
                "do not inherit the managed cache selection.",
                "schema COMMAND includes output field hints for row-based responses.",
                "Notebook selectors accept an exact UUID, full path, or unique exact name.",
                "Follow next_offset or next_page_offset; pin snapshot with --expect-snapshot.",
                "For recent edits, sync once per request and pin its snapshot_id across reads.",
                "Check source labels, coverage, warnings, and truncated when interpreting results.",
                "--text indents JSON; --fields selects row fields or top-level keys.",
                "Output keeps essential evidence by default; --verbose includes full metadata.",
                "--fields selects from full rows or responses, overriding concise defaults.",
                "For explanation or learning, use bundle and open its images, including diagrams.",
                "read is a rough text overview; read/search/bundle exclude handwriting candidates "
                "unless --source handwriting or all is requested.",
                "pages includes recognition counts, not candidate text. Use these to prioritize "
                "inspection, never to exclude pages with diagrams or unindexed ink.",
                "Advanced commands remain available with COMMAND --help or schema COMMAND.",
                "Source files are read-only; some commands write local copies or exports.",
            ],
            "annotations": (
                "--layer native reconstructs supported copied pen/highlighter records with "
                "coverage diagnostics. It requires a compatible RocksDB runtime and writable "
                "scratch storage. Full visual fidelity is unverified. --layer annotated uses "
                "a PDF registered with attach-pdf. A successful native query with no matching "
                "records renders the background with no-native-records-observed coverage; "
                "absence of annotations in Goodnotes remains unverified."
            ),
            "exit_codes": {
                "0": "success",
                "1": "operation failed",
                "2": "invalid arguments",
                "130": "interrupted or broken pipe",
            },
        },
        settings(ctx),
    )


def parameter_contract(parameter: Any) -> dict[str, Any]:
    """Describe registered parser metadata without maintaining a second option table."""
    result = {
        "name": parameter.name,
        "type": parameter.type.name,
        "required": parameter.required,
        "default": parameter.default,
    }
    if parameter.param_type_name == "option":
        result["flags"] = parameter.opts + parameter.secondary_opts
        result["is_flag"] = parameter.is_flag
    for key in ("help", "envvar"):
        value = getattr(parameter, key, None)
        if value is not None:
            result[key] = value
    if getattr(parameter, "hidden", False):
        result["advanced"] = True
    for key in ("min", "max"):
        value = getattr(parameter.type, key, None)
        if value is not None:
            result[key] = value
    return result


def main() -> None:

    try:
        exit_code = app(standalone_mode=False)
        if isinstance(exit_code, int):
            raise SystemExit(exit_code)
    except typer.TyperException as exc:
        emit_error("invalid_arguments", exc.format_message())
        raise SystemExit(2) from None
    except GoodnotesError as exc:
        emit_error(
            getattr(exc, "code", "operation_failed"),
            str(exc),
            details=getattr(exc, "details", None),
        )
        raise SystemExit(1) from None
    except (BrokenPipeError, KeyboardInterrupt):
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
