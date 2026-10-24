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
    help="Read and search Goodnotes. Compact JSON by default. Start with sync or schema.",
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
    fields: Annotated[str | None, typer.Option(help="Comma-separated output fields.")] = None,
    expect_snapshot: Annotated[
        str | None, typer.Option(help="Reject a changed pagination generation.")
    ] = None,
    show_version: Annotated[
        bool, typer.Option("--version", callback=version, is_eager=True)
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
    """Refresh the local read view from Goodnotes files."""

    cfg = settings(ctx)
    emit(
        refresh_cache(
            library or default_library(), cfg.data_dir or default_data_dir(), force=force
        ),
        cfg,
    )


@command()
def snapshot(ctx: typer.Context, library: Path, destination: Path) -> None:
    """Create a named, independent copy of a Library directory."""

    result = create_snapshot(library, destination)
    emit({"snapshot": result, "status": "ready"}, settings(ctx))


@command("ls")
def list_items(
    ctx: typer.Context,
    folder: str = "/",
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List a folder's direct children. Follow next_offset for more."""
    with repository(ctx) as lib:
        emit(window(lib.list_items(folder), limit, offset), settings(ctx))


@command()
def tree(
    ctx: typer.Context,
    folder: str = "/",
    depth: Annotated[int, typer.Option(min=1, max=50)] = 3,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List descendants in stored order, bounded by depth and result count."""
    with repository(ctx) as lib:
        emit(window(lib.tree(folder, max_depth=depth), limit, offset), settings(ctx))


@command()
def info(ctx: typer.Context, notebook: str) -> None:
    """Resolve a notebook path, UUID, or unique name."""
    with repository(ctx) as lib:
        emit(lib.resolve(notebook), settings(ctx))


@command()
def pages(
    ctx: typer.Context,
    notebook: str,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List one-based pages in current notebook order."""
    with repository(ctx) as lib:
        result = window(lib.pages(notebook), limit, offset)
        result["pages"] = result.pop("items")
        emit(result, settings(ctx))


@command("read")
def read_text(
    ctx: typer.Context,
    notebook: str,
    page: Annotated[int | None, typer.Option(min=1)] = None,
    pages: Annotated[str | None, typer.Option(help="Up to 20 pages, e.g. 1-3,7.")] = None,
    source: Annotated[str, typer.Option(help="all, pdf, handwriting, or textbox.")] = "all",
    max_chars: Annotated[int, typer.Option(min=1, max=1000000)] = 12000,
) -> None:
    """Read page text with separate PDF, handwriting, and textbox sources."""
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
    query: str,
    folder: str = "/",
    source: str = "all",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Find literal text. Return short snippets and page identifiers."""
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
    query: str,
    folder: str = "/",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    page_limit: Annotated[int, typer.Option(min=1, max=250)] = 200,
    page_offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Search PDF backgrounds when the stored index misses a note."""
    with repository(ctx) as lib:
        result = lib.scan_pdfs(
            query, folder=folder, limit=limit, page_limit=page_limit, page_offset=page_offset
        )
        result["hits"] = snippet_hits(result["hits"], query)
        emit(result, settings(ctx))


@command("export")
def export_notebook(
    ctx: typer.Context,
    notebook: str,
    output: Annotated[Path, typer.Option(help="New PDF file to write.")],
    layer: Annotated[str, typer.Option(help="background or annotated.")] = "background",
) -> None:
    """Export all notebook pages in their current order."""

    with repository(ctx) as lib:
        emit(
            export_pdf(lib.sandbox, lib.pages(notebook), output, layer=layer),
            settings(ctx),
        )


@command()
def render(
    ctx: typer.Context,
    notebook: str,
    output: Annotated[Path, typer.Option(help="New PNG file to write.")],
    page: Annotated[int, typer.Option(min=1)] = 1,
    layer: str = "background",
    dpi: Annotated[int, typer.Option(min=72, max=300)] = 150,
) -> None:
    """Render a page image for visual reading by an agent."""

    with repository(ctx) as lib:
        all_pages = lib.pages(notebook)
        number = select_pages(str(page), len(all_pages))[0]
        emit(
            render_page(lib.sandbox, all_pages[number - 1], output, layer=layer, dpi=dpi),
            settings(ctx),
        )


@command("attach-pdf")
def attach_pdf(ctx: typer.Context, notebook: str, pdf: Path) -> None:
    """Register a full Goodnotes PDF export for annotated rendering."""

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
    notebook: str,
    output: Annotated[Path, typer.Option(help="New output directory.")],
    pages: str = "1",
    layer: str = "background",
    dpi: Annotated[int, typer.Option(min=72, max=300)] = 150,
    max_chars: Annotated[int, typer.Option(min=1, max=1000000)] = 12000,
) -> None:
    """Write selected page images and text.json for agent inspection."""

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
    """Check the local read view and report unavailable capabilities."""
    with repository(ctx) as lib:
        emit(lib.stats(), settings(ctx))


@command()
def schema(ctx: typer.Context) -> None:
    """Print a small command contract without loading any notes."""
    emit(
        {
            "version": __version__,
            "format": "compact-json",
            "page_numbers": "one-based",
            "commands": {
                "sync": "[--library PATH --force]",
                "ls": "[FOLDER] [--limit N --offset N]",
                "tree": "[FOLDER] [--depth N --limit N --offset N]",
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
                "schema": "",
            },
            "limits": {
                "read_pages": 20,
                "read_chars": 12000,
                "search_hits": 20,
                "snippet_chars": 320,
                "list_items": 100,
                "bundle_pages": 10,
            },
            "global_options": [
                "--fields FIELDS",
                "--data-dir PATH",
                "--sandbox PATH",
                "--json",
                "--text",
                "--expect-snapshot ID",
            ],
            "annotations": "Native ink rendering unavailable. Annotated layers require attach-pdf.",
            "exit_codes": {"0": "success", "1": "operation failed", "2": "invalid arguments"},
        },
        settings(ctx),
    )


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
