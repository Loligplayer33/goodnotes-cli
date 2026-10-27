"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .. import build_info
from ..config import default_data_dir
from ..errors import (
    GoodnotesError,
)
from ..presentation.json import emit
from .core import command, repository, repository_query, settings


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
        from ..diagnostics import inspect_environment
        from ..native import runtime_status

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
        from ..native import runtime_status

        result["native_runtime"] = runtime_status()
        emit(result, cfg)
