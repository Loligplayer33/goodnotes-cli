"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from . import __version__
from .cache import read_view, refresh_cache
from .config import Settings, default_data_dir, default_library
from .documents import export_pdf, register_annotated_pdf, render_page
from .errors import GoodnotesError
from .library import Library
from .output import (
    bound_page_text,
    emit,
    emit_error,
    select_pages,
    serializable,
    snippet_hits,
    window,
)
from .snapshot import create_snapshot

ItemSelector = Annotated[
    str, typer.Argument(help="Exact UUID, full library path, or unique exact name.")
]
FolderOption = Annotated[
    str, typer.Option(help="Folder UUID, path, or unique exact name; / is the root.")
]


class AgentGroup(TyperGroup):
    """Keep expected errors machine-readable, including command parsing errors."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().main(*args, **kwargs)
        except GoodnotesError as exc:
            emit_error(getattr(exc, "code", "operation_failed"), str(exc))
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
        @wraps(function)
        def checked(*fargs: Any, **fkwargs: Any) -> Any:
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
        Path | None, typer.Option(envvar="GOODNOTES_DATA_DIR", help="CLI cache directory.")
    ] = None,
    text: Annotated[bool, typer.Option("--text", help="Indent JSON for inspection.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Compact JSON, the default.")] = False,
    fields: Annotated[
        str | None, typer.Option(help="Comma-separated row fields or top-level response keys.")
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
        with Library(sandbox) as lib:
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

    Example: goodnotes sync --library /path/to/Library
    """

    cfg = settings(ctx)
    emit(
        refresh_cache(
            library or default_library(), cfg.data_dir or default_data_dir(), force=force
        ),
        cfg,
    )


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
) -> None:
    """List pages in current notebook order, numbered from 1.

    Returns pages and next_offset. Continue with --offset and use global
    --expect-snapshot to keep results on the same generation.

    Example: goodnotes pages "Course Notes" --limit 20
    """
    with repository(ctx) as lib:
        result = window(lib.pages(notebook), limit, offset)
        result["pages"] = result.pop("items")
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
    folder: FolderOption = "/",
    source: Annotated[str, typer.Option(help="all, pdf, handwriting, or textbox.")] = "all",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Search stored text for a literal, case-insensitive phrase.

    --source accepts all, pdf, handwriting, or textbox. Returns hits, next_offset,
    coverage, and warnings. No hits do not prove absence; scan-pdfs searches PDF
    backgrounds separately. Snippets are capped at 320 characters.

    Example: goodnotes search "regression" --folder "Course" --limit 10
    """
    with repository(ctx) as lib:
        hits = lib.search(query, folder=folder, source=source, limit=offset + limit + 1)
        selected = snippet_hits(hits[offset : offset + limit], query)
        emit(
            {
                "hits": selected,
                "next_offset": offset + limit if len(hits) > offset + limit else None,
                "coverage": "stored-index",
                "warnings": lib.text_warnings(),
            },
            settings(ctx),
        )


@command("scan-pdfs")
def scan_pdfs(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Literal, case-insensitive PDF text to find.")],
    folder: FolderOption = "/",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    page_limit: Annotated[int, typer.Option(min=1, max=250)] = 200,
    page_offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Search PDF background text when the stored index misses it.

    Scans a bounded number of pages. Continue using next_page_offset as --page-offset.
    Check warnings for incomplete coverage. Does not search handwriting or attached PDFs.

    Example: goodnotes scan-pdfs "regression" --folder "Course" --page-limit 50
    """
    with repository(ctx) as lib:
        result = lib.scan_pdfs(
            query, folder=folder, limit=limit, page_limit=page_limit, page_offset=page_offset
        )
        result["hits"] = snippet_hits(result["hits"], query)
        emit(result, settings(ctx))


@command("export")
def export_notebook(
    ctx: typer.Context,
    notebook: ItemSelector,
    output: Annotated[Path, typer.Option(help="New PDF file to write.")],
    layer: Annotated[str, typer.Option(help="background or annotated.")] = "background",
) -> None:
    """Export all notebook pages in their current order to a new PDF.

    The default background layer omits native ink. The annotated layer requires
    attach-pdf and uses an unverified user export. Existing outputs are never replaced.

    Example: goodnotes export "Course Notes" --output /tmp/course.pdf
    """

    with repository(ctx) as lib:
        emit(
            export_pdf(lib.sandbox, lib.pages(notebook), output, layer=layer),
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

    --layer accepts background or annotated. Background omits native ink; annotated
    requires attach-pdf. Page numbers start at 1. Existing outputs are never replaced.

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
    covers all text. --layer accepts background or annotated; annotated requires
    attach-pdf. Text comes from read sources regardless of the image layer.

    Example: goodnotes bundle "Course Notes" --pages 1-3 --output /tmp/course-pages
    """

    cfg = settings(ctx)
    with repository(ctx) as lib:
        sandbox = lib.sandbox
        destination = sandbox.ensure_output(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".goodnotes-bundle-", dir=destination.parent))
        try:
            all_pages = lib.pages(notebook)
            selected = select_pages(pages, len(all_pages), maximum=10)
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
                json.dumps({"pages": manifest, "truncated": truncated}, ensure_ascii=False)
            )
            publish_bundle(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        emit(
            {
                "directory": destination,
                "manifest": destination / "text.json",
                "pages": selected,
                "layer": layer,
                "truncated": truncated,
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
def doctor(ctx: typer.Context) -> None:
    """Report local library counts, missing metadata, and text-source warnings.

    Inspects the selected copy without refreshing it or contacting Goodnotes.

    Example: goodnotes doctor
    """
    with repository(ctx) as lib:
        emit(lib.stats(), settings(ctx))


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
            emit(
                {
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
                },
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
                "snapshot": "LIBRARY DESTINATION",
                "ls": "[--folder FOLDER --limit N --offset N]",
                "tree": "[--folder FOLDER --depth N --limit N --offset N]",
                "info": "NOTEBOOK",
                "pages": "NOTEBOOK [--limit N --offset N]",
                "read": (
                    "NOTEBOOK [--page N | --pages 1-3,7] "
                    "[--source all|pdf|handwriting|textbox] [--max-chars N]"
                ),
                "search": "QUERY [--folder FOLDER --source SOURCE --limit N --offset N]",
                "scan-pdfs": "QUERY [--folder FOLDER --page-limit N --page-offset N --limit N]",
                "export": "NOTEBOOK --output FILE [--layer background|annotated]",
                "render": "NOTEBOOK --output PNG [--page N --layer LAYER --dpi N]",
                "bundle": "NOTEBOOK --output DIR [--pages 1-3 --layer LAYER --dpi N --max-chars N]",
                "attach-pdf": "NOTEBOOK PDF",
                "doctor": "",
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
                "Notebook selectors accept an exact UUID, full path, or unique exact name.",
                "Follow next_offset or next_page_offset; pin snapshot with --expect-snapshot.",
                "Check source labels, coverage, warnings, and truncated when interpreting results.",
                "--text indents JSON; --fields selects row fields or top-level keys.",
                "Source files are read-only; some commands write local copies or exports.",
            ],
            "annotations": "Native ink rendering unavailable. Annotated layers require attach-pdf.",
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
        app(standalone_mode=False)
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
