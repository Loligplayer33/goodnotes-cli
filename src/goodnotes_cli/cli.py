"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from typer.core import TyperGroup

from . import __version__
from .cache import read_view, refresh_cache
from .config import Settings, default_data_dir, default_library
from .documents import export_pdf, register_annotated_pdf, render_page
from .errors import (
    CacheAccessError,
    FilesystemAccessError,
    GoodnotesError,
    TextIndexUnavailableError,
)
from .library import Library
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


app = typer.Typer(
    cls=AgentGroup,
    help=(
        "Browse, read, search, and export a copied Goodnotes library. Compact JSON by default.\n\n"
        "Put global options before the command. Use COMMAND --help for examples and options, "
        "or schema COMMAND for machine-readable details. Start with sync to create a read view."
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
                    frozenset(name for name, value in ctx.params.items() if value is True),
                )
                cfg.projection = projection
                validate_projection(cfg.fields, projection)
            try:
                return function(*fargs, **fkwargs)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise GoodnotesError(str(exc)) from exc

        return app.command(*args, **kwargs)(checked)

    return decorate


def version(value: bool) -> None:
    if value:
        typer.echo(__version__)
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
    text: Annotated[bool, typer.Option("--text", help="Indent JSON for inspection.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Compact JSON, the default.")] = False,
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
    ctx.obj = Settings(sandbox, data_dir, text and not json_output, fields, expect_snapshot)


def settings(ctx: typer.Context) -> Settings:
    return ctx.obj


@contextmanager
def repository(ctx: typer.Context) -> Iterator[Library]:
    cfg = settings(ctx)
    with read_view(cfg) as sandbox:
        cfg.snapshot_id = sandbox.root.name
        with ExitStack() as stack:
            try:
                lib = stack.enter_context(Library(sandbox))
            except sqlite3.Error as exc:
                raise CacheAccessError(
                    "cache_invalid",
                    f"Could not read the copied library catalog: {sandbox.projection_path}",
                    operation="read_snapshot_catalog",
                    path=sandbox.projection_path,
                    cache_dir=cfg.cache_dir,
                ) from exc
            yield lib


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


@command()
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


@command()
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
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List a folder's direct children.

    --folder accepts a path, UUID, or unique name; / selects the root. Continue with
    next_offset and pin the returned snapshot ID using global --expect-snapshot.

    Example: goodnotes ls --folder "Course" --limit 20
    """
    with repository(ctx) as lib:
        emit(window(lib.list_items(folder), limit, offset), settings(ctx))


@command()
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
        emit(window(lib.tree(folder, max_depth=depth), limit, offset), settings(ctx))


@command()
def info(ctx: typer.Context, notebook: ItemSelector) -> None:
    """Show a folder or notebook's metadata.

    NOTEBOOK accepts an exact UUID, full library path, or unique exact name.
    Ambiguous names fail; use a full path or UUID to disambiguate.

    Example: goodnotes info "Course Notes"
    """
    with repository(ctx) as lib:
        emit(lib.resolve(notebook), settings(ctx))


@command()
def pages(
    ctx: typer.Context,
    notebook: ItemSelector,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    with_recognition: Annotated[
        bool,
        typer.Option(help="Include stored handwriting recognition availability and index state."),
    ] = False,
) -> None:
    """List pages in current notebook order, numbered from 1.

    Returns pages and next_offset. --with-recognition adds metadata without reading
    candidate text. annotation_count counts projected note items, not ink or recognition.
    Continue with --offset and pin --expect-snapshot for a consistent generation.

    Example: goodnotes pages "Course Notes" --limit 20
    """
    with repository(ctx) as lib:
        result = window(lib.pages(notebook), limit, offset)
        result["pages"] = result.pop("items")
        if with_recognition:
            recognition = lib.recognition_status(notebook)
            result["pages"] = [
                {**serializable(page), "recognition": serializable(recognition[page.id])}
                for page in result["pages"]
            ]
        result["annotation_count_source"] = "projection.notes_items"
        emit(result, settings(ctx))


@command("read")
def read_text(
    ctx: typer.Context,
    notebook: ItemSelector,
    page: Annotated[int | None, typer.Option(min=1)] = None,
    pages: Annotated[str | None, typer.Option(help="Up to 20 pages, e.g. 1-3,7.")] = None,
    source: Annotated[str, typer.Option(help="all, pdf, handwriting, or textbox.")] = "all",
    max_chars: Annotated[int, typer.Option(min=1, max=1000000)] = 12000,
) -> None:
    """Read page text with PDF, handwriting, and textbox source labels.

    Defaults to page 1. Choose --page or --pages, up to 20 distinct pages. The
    character budget covers all selected pages; check truncated in the response.
    Handwriting is stored recognition output. Native ink and attached PDFs are not decoded.

    Example: goodnotes read "Course Notes" --pages 1-3,7 --source handwriting
    """
    if page is not None and pages is not None:
        raise GoodnotesError("Use --page or --pages, not both.")
    if source not in {"all", "pdf", "handwriting", "textbox"}:
        raise GoodnotesError("--source must be all, pdf, handwriting, or textbox.")
    with repository(ctx) as lib:
        selected = select_pages(pages or str(page or 1), len(lib.pages(notebook)))
        results = []
        remaining = max_chars
        for number in selected:
            data = serializable(lib.read_page(notebook, number, source=source, max_chars=remaining))
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
    source: Annotated[str, typer.Option(help="all, pdf, handwriting, or textbox.")] = "all",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Search stored text for a literal, case-insensitive phrase.

    Choose --notebook for one notebook or --folder for a subtree. coverage_detail
    reports scoped source availability, not proof that every page was indexed.
    No hits do not prove absence; scan-pdfs searches PDF backgrounds separately.
    Snippets are capped at 320 characters.

    Example: goodnotes search "regression" --folder "Course" --limit 10
    """
    selected_folder = _search_folder(folder, notebook)
    with repository(ctx) as lib:
        try:
            hits = lib.search(
                query,
                folder=selected_folder,
                notebook=notebook,
                source=source,
                limit=offset + limit + 1,
            )
        except TextIndexUnavailableError as exc:
            coverage = lib.search_coverage(folder=selected_folder, notebook=notebook, source=source)
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
        coverage = lib.search_coverage(folder=selected_folder, notebook=notebook, source=source)
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


@command("scan-pdfs")
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
        result = lib.scan_pdfs(
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
    layer: Annotated[str, typer.Option(help="background, native, or annotated.")] = "background",
    pages: Annotated[
        str | None,
        typer.Option(help="Selected pages in listed order, e.g. 1-3,7. Defaults to all."),
    ] = None,
) -> None:
    """Export all pages, or selected pages in listed order, to a new PDF.

    The default background layer omits native ink. The annotated layer requires
    attach-pdf and uses an unverified user export. Existing outputs are never replaced.

    Example: goodnotes export "Course Notes" --output /tmp/course.pdf
    """

    with repository(ctx) as lib:
        all_pages = lib.pages(notebook)
        selected = (
            select_pages(pages, len(all_pages), maximum=len(all_pages))
            if pages is not None
            else None
        )
        emit(
            export_pdf(lib.sandbox, all_pages, output, layer=layer, page_numbers=selected),
            settings(ctx),
        )


@command()
def render(
    ctx: typer.Context,
    notebook: ItemSelector,
    output: Annotated[Path, typer.Option(help="New PNG file to write.")],
    page: Annotated[int, typer.Option(min=1)] = 1,
    layer: str = "background",
    dpi: Annotated[int, typer.Option(min=72, max=300)] = 150,
) -> None:
    """Render one page to a new PNG for visual inspection.

    --layer native reconstructs supported copied ink and reports coverage. Background
    omits native ink; annotated requires attach-pdf. Page numbers start at 1.
    Existing outputs are never replaced.

    Example: goodnotes render "Course Notes" --page 3 --output /tmp/page-3.png
    """

    with repository(ctx) as lib:
        all_pages = lib.pages(notebook)
        number = select_pages(str(page), len(all_pages))[0]
        emit(
            render_page(lib.sandbox, all_pages[number - 1], output, layer=layer, dpi=dpi),
            settings(ctx),
        )


@command("attach-pdf")
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
        item = lib.resolve(notebook, kind="notebook")
        emit(
            register_annotated_pdf(
                lib.sandbox, item.id, pdf, expected_pages=len(lib.pages(notebook))
            ),
            settings(ctx),
        )


@command()
def bundle(
    ctx: typer.Context,
    notebook: ItemSelector,
    output: Annotated[Path, typer.Option(help="New output directory.")],
    pages: str = "1",
    layer: str = "background",
    dpi: Annotated[int, typer.Option(min=72, max=300)] = 150,
    max_chars: Annotated[int, typer.Option(min=1, max=1000000)] = 12000,
) -> None:
    """Save page images and source-labeled text.json in a new directory.

    Select up to 10 distinct pages with --pages, e.g. 1-3,7. One character budget
    covers all text. --layer native reconstructs supported copied ink and reports
    coverage; annotated requires attach-pdf. Text keeps its original source labels.

    Example: goodnotes bundle "Course Notes" --pages 1-3 --output /tmp/course-pages
    """

    cfg = settings(ctx)
    with repository(ctx) as lib:
        sandbox = lib.sandbox
        all_pages = lib.pages(notebook)
        selected = select_pages(pages, len(all_pages), maximum=10)
        destination = sandbox.ensure_output(output)
        parent_existed = destination.parent.exists()
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".goodnotes-bundle-", dir=destination.parent))
            manifest = []
            remaining = max_chars
            for number in selected:
                filename = f"page-{number:04d}.png"
                rendered = render_page(
                    sandbox, all_pages[number - 1], temporary / filename, layer=layer, dpi=dpi
                )
                text = serializable(lib.read_page(notebook, number, max_chars=remaining))
                remaining -= sum(len(part["text"]) for part in text["parts"])
                manifest.append(
                    {
                        "page": number,
                        "image": filename,
                        "render": {**serializable(rendered), "path": str(destination / filename)},
                        "text": text,
                    }
                )
            truncated = bound_page_text([entry["text"] for entry in manifest], max_chars)
            (temporary / "text.json").write_text(
                json.dumps(
                    {
                        "snapshot": cfg.snapshot_id,
                        "captured_at": sandbox.captured_at,
                        "pages": manifest,
                        "truncated": truncated,
                    },
                    ensure_ascii=False,
                )
            )
            publish_bundle(temporary, destination)
        except BaseException as exc:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            if not parent_existed:
                try:
                    destination.parent.rmdir()
                except OSError:
                    pass
            if isinstance(exc, OSError):
                raise FilesystemAccessError.from_exception(
                    exc, operation="write_bundle_output", path=destination, kind="output"
                ) from exc
            raise
        emit(
            {
                "directory": destination,
                "manifest": destination / "text.json",
                "pages": selected,
                "layer": layer,
                "truncated": truncated,
                "captured_at": sandbox.captured_at,
            },
            cfg,
        )


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
        result = lib.stats()
        result["storage"] = {
            "cache_dir": cfg.cache_dir,
            "snapshot_path": lib.sandbox.root,
            "captured_at": lib.sandbox.captured_at,
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
    emit(
        {
            "version": __version__,
            "format": "compact-json",
            "page_numbers": "one-based",
            "commands": {
                "sync": "[--library PATH --force]",
                "watch": "[--library PATH --interval SECONDS --debounce SECONDS --iterations N]",
                "snapshot": "LIBRARY DESTINATION",
                "ls": "[--folder FOLDER --limit N --offset N]",
                "tree": "[--folder FOLDER --depth N --limit N --offset N]",
                "info": "NOTEBOOK",
                "pages": "NOTEBOOK [--with-recognition --limit N --offset N]",
                "read": (
                    "NOTEBOOK [--page N | --pages 1-3,7] "
                    "[--source all|pdf|handwriting|textbox] [--max-chars N]"
                ),
                "search": (
                    "QUERY [--folder FOLDER | --notebook NOTEBOOK] "
                    "[--source SOURCE --limit N --offset N]"
                ),
                "scan-pdfs": (
                    "QUERY [--folder FOLDER | --notebook NOTEBOOK] "
                    "[--page-limit N --page-offset N --limit N]"
                ),
                "export": (
                    "NOTEBOOK --output FILE [--pages 1-3,7 --layer background|native|annotated]"
                ),
                "render": "NOTEBOOK --output PNG [--page N --layer LAYER --dpi N]",
                "bundle": "NOTEBOOK --output DIR [--pages 1-3 --layer LAYER --dpi N --max-chars N]",
                "attach-pdf": "NOTEBOOK PDF",
                "doctor": "[--access-only --library PATH --scratch-dir PATH --output-dir PATH]",
                "schema": "[COMMAND]",
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
                "--fields FIELDS",
                "--data-dir PATH",
                "--sandbox PATH",
                "--json",
                "--text",
                "--expect-snapshot ID",
                "--version",
                "--help",
            ],
            "usage_notes": [
                "Global options go before the command. schema COMMAND gives targeted details.",
                "schema COMMAND includes output field hints for row-based responses.",
                "Notebook selectors accept an exact UUID, full path, or unique exact name.",
                "Follow next_offset or next_page_offset; pin snapshot with --expect-snapshot.",
                "For recent edits, sync once per request and pin its snapshot_id across reads.",
                "Check source labels, coverage, warnings, and truncated when interpreting results.",
                "--text indents JSON; --fields selects row fields or top-level keys.",
                "Source files are read-only; some commands write local copies or exports.",
            ],
            "annotations": (
                "--layer native reconstructs supported copied pen/highlighter records with "
                "coverage diagnostics. It requires a compatible RocksDB runtime and writable "
                "scratch storage. Full visual fidelity is unverified. --layer annotated uses "
                "a PDF registered with attach-pdf."
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
        emit_error(getattr(exc, "code", "operation_failed"), str(exc))
        raise SystemExit(1) from None
    except (BrokenPipeError, KeyboardInterrupt):
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
