"""Atomic no-overwrite publication for generated files."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from ..errors import FilesystemAccessError, GoodnotesError


def _publish_new_file(
    destination: Path,
    writer: Callable[[str], object],
) -> None:
    """Write to a sibling temporary file, then create the destination without overwriting."""
    temporary = destination.with_name(
        f".{destination.stem}.tmp-{os.getpid()}-{uuid.uuid4().hex}{destination.suffix}"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer(str(temporary))
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise GoodnotesError("The document renderer did not create an output file.")
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise GoodnotesError(f"The output file already exists: {destination}") from exc
    except GoodnotesError:
        raise
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="write_document_output", path=destination, kind="output"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


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
