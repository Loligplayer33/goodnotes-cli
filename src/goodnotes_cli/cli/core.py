"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer
from typer.core import TyperGroup

from .. import __version__, build_info
from ..cache import read_view
from ..config import Settings
from ..errors import (
    CacheAccessError,
    GoodnotesError,
)
from ..presentation.contracts import Projection, validate_projection
from ..presentation.json import emit_error

if TYPE_CHECKING:
    from ..library import Library


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
                if (
                    command_name == "doctor"
                    and fkwargs.get("access_only")
                    and fkwargs.get("notebook") is not None
                ):
                    raise GoodnotesError("--notebook cannot be combined with --access-only.")
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
                from ..library import Library

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
