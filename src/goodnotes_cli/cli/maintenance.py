"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ..cache import refresh_cache
from ..config import default_data_dir, default_library
from ..errors import (
    GoodnotesError,
)
from ..presentation.json import emit
from ..storage.capture.lifecycle import create_snapshot
from ..watch import watch_library
from .core import command, settings


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
