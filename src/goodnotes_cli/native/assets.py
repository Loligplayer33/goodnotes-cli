"""Read copied native image attachments through their projection bindings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
from pathlib import Path
from typing import Protocol

from ..errors import GoodnotesError
from ..storage.paths import _require_not_production, path_contains, resolve_confined_file, stat_key
from ..storage.sandbox import open_database
from .errors import NativeError
from .raster import (
    _IMAGE_READ_CHUNK,
    _MAX_IMAGE_BYTES,
    NativeImageData,
    _decode_native_image,
    _native_failure,
)

_MAX_ATTACHMENT_ROWS = 10_000
_MAX_STORED_PATH_BYTES = 4096
_MAX_ATTACHMENT_SCAN_ENTRIES = 200_000
_FINGERPRINT_VERSION = "native-attachments-v1"


class AttachmentSandbox(Protocol):
    """The validated paths needed to read one copied attachment."""

    @property
    def root(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...

    @property
    def attachments_dir(self) -> Path: ...

    @property
    def projection_path(self) -> Path: ...


def native_attachment_fingerprint(
    sandbox: AttachmentSandbox, document_id: str
) -> tuple[object, ...]:
    """Return a conservative, content-free signature for a notebook's copied assets.

    The projection and attachment files are never opened for writing. Missing rows,
    unavailable bindings, and unsafe unused paths remain representable so an unrelated
    incomplete attachment does not prevent native pen rendering.
    """

    if not _bounded_identifier(document_id):
        raise _native_failure(
            "The requested notebook binding exceeds the safe metadata limit.",
            operation="fingerprint_native_attachments",
        )
    projection_signature = _projection_signature(sandbox)
    attachment_root, root_signature = _attachment_root(sandbox)
    try:
        with open_database(sandbox.projection_path) as connection:
            columns = _attachment_columns(connection)
            required = {"id", "path", "available", "document_id"}
            if not required.issubset(columns):
                schema_state: object = ("schema-unavailable", tuple(sorted(columns)))
                rows: list[sqlite3.Row] = []
            else:
                schema_state = "schema-available"
                rows = connection.execute(
                    "SELECT id, path, available FROM attachments "
                    "WHERE document_id = ? ORDER BY id, path LIMIT ?",
                    (document_id, _MAX_ATTACHMENT_ROWS + 1),
                ).fetchall()
    except sqlite3.Error as exc:
        raise _native_failure(
            "Could not read attachment bindings from the copied projection.",
            operation="fingerprint_native_attachments",
        ) from exc

    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while attachment metadata was read.",
            operation="fingerprint_native_attachments",
        )
    if len(rows) > _MAX_ATTACHMENT_ROWS:
        raise _native_failure(
            "The copied projection has too many image bindings to fingerprint safely.",
            operation="fingerprint_native_attachments",
        )

    document_signature = _metadata_digest((document_id,))
    if schema_state != "schema-available":
        _ensure_root_unchanged(attachment_root, root_signature)
        return (
            _FINGERPRINT_VERSION,
            projection_signature,
            root_signature,
            document_signature,
            schema_state,
        )

    basename_map: dict[str, list[Path]] | None = None
    bindings: list[tuple[object, ...]] = []
    for row in rows:
        raw_path = row["path"]
        if isinstance(raw_path, str):
            state, file_signature, basename_map = _fingerprint_path(
                attachment_root,
                raw_path,
                basename_map=basename_map,
            )
        elif raw_path is None:
            state, file_signature = "missing", None
        else:
            state, file_signature = "unavailable", None
        metadata_signature = _metadata_digest((row["id"], raw_path, row["available"]))
        bindings.append((metadata_signature, state, file_signature))

    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while attachment metadata was read.",
            operation="fingerprint_native_attachments",
        )
    _ensure_root_unchanged(attachment_root, root_signature)
    return (
        _FINGERPRINT_VERSION,
        projection_signature,
        root_signature,
        document_signature,
        schema_state,
        tuple(bindings),
    )


def load_native_image(
    sandbox: AttachmentSandbox, document_id: str, attachment_id: str
) -> NativeImageData:
    """Read and verify one available PNG/JPEG bound to this copied notebook."""

    if not _bounded_identifier(document_id) or not _bounded_identifier(attachment_id):
        raise _native_failure(
            "The requested image binding exceeds the safe metadata limit.",
            operation="load_native_image_binding",
        )
    projection_signature = _projection_signature(sandbox)
    attachment_root, root_signature = _attachment_root(sandbox)
    row = _load_attachment_binding(sandbox, document_id, attachment_id)
    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while the image binding was checked.",
            operation="load_native_image_binding",
        )

    if not _is_available(row["available"]):
        raise _native_failure(
            "The referenced copied image attachment is unavailable.",
            operation="load_native_image_binding",
        )
    raw_path = row["path"]
    if not isinstance(raw_path, str) or not raw_path:
        raise _native_failure(
            "The referenced copied image has no usable attachment path.",
            operation="resolve_native_image",
        )

    resolved, state, _ = _resolve_attachment_path(attachment_root, raw_path)
    if resolved is None:
        descriptions = {
            "missing": "The referenced copied image is missing.",
            "unsafe": (
                "The referenced copied image path is unsafe or outside the attachment folder."
            ),
            "ambiguous": "The referenced copied image path has an ambiguous basename.",
            "unavailable": "The referenced copied image could not be inspected safely.",
        }
        raise _native_failure(
            descriptions.get(state, "The referenced copied image is unavailable."),
            operation="resolve_native_image",
        )

    data, expected_signature = _read_stable_image_file(attachment_root, resolved)
    second_path, second_state, _ = _resolve_attachment_path(attachment_root, raw_path)
    if second_state != "available" or second_path != resolved:
        raise _native_failure(
            "The copied image binding changed while the attachment was read.",
            operation="read_native_image",
        )
    _ensure_root_unchanged(
        attachment_root,
        root_signature,
        operation="read_native_image",
    )
    try:
        second_signature = stat_key(second_path.stat())
    except OSError as exc:
        raise _native_failure(
            "The copied image changed while the attachment was read.",
            operation="read_native_image",
        ) from exc
    if second_signature != expected_signature:
        raise _native_failure(
            "The copied image changed while the attachment was read.",
            operation="read_native_image",
        )
    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while the image was read.",
            operation="read_native_image",
        )

    return _decode_native_image(data)


def _load_attachment_binding(
    sandbox: AttachmentSandbox, document_id: str, attachment_id: str
) -> sqlite3.Row:
    try:
        with open_database(sandbox.projection_path) as connection:
            columns = _attachment_columns(connection)
            required = {"id", "path", "available", "document_id"}
            if not required.issubset(columns):
                raise _native_failure(
                    "The copied projection cannot verify this image's notebook binding.",
                    operation="load_native_image_binding",
                )
            rows = connection.execute(
                "SELECT id, path, available FROM attachments "
                "WHERE document_id = ? AND id = ? ORDER BY id, path LIMIT 2",
                (document_id, attachment_id),
            ).fetchall()
    except NativeError:
        raise
    except sqlite3.Error as exc:
        raise _native_failure(
            "Could not read the referenced image binding from the copied projection.",
            operation="load_native_image_binding",
        ) from exc

    if len(rows) != 1:
        raise _native_failure(
            "The referenced image has no unique binding to this notebook.",
            operation="load_native_image_binding",
        )
    return rows[0]


def _attachment_columns(connection: sqlite3.Connection) -> set[str]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'attachments'"
    ).fetchone()
    if table is None:
        return set()
    return {row["name"] for row in connection.execute("PRAGMA table_info(attachments)")}


def _is_available(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def _bounded_identifier(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= _MAX_STORED_PATH_BYTES
    except UnicodeError:
        return False


def _projection_signature(sandbox: AttachmentSandbox) -> tuple[int, int, int, int, int, int]:
    path = Path(sandbox.projection_path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise _native_failure(
            "The copied projection could not be inspected safely.",
            operation="inspect_native_image_projection",
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise _native_failure(
            "The copied projection is not a regular file.",
            operation="inspect_native_image_projection",
        )
    return stat_key(info)


def _attachment_root(
    sandbox: AttachmentSandbox,
) -> tuple[Path, tuple[int, int, int, int, int, int]]:
    requested = Path(sandbox.attachments_dir)
    try:
        requested_info = requested.lstat()
        root = requested.resolve(strict=True)
        root_info = root.lstat()
        snapshot_root = Path(sandbox.root).resolve(strict=True)
        library_root = Path(sandbox.library_dir).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _native_failure(
            "The copied attachment folder could not be inspected safely.",
            operation="inspect_native_image_attachments",
        ) from exc
    if (
        stat.S_ISLNK(requested_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or not path_contains(snapshot_root, root)
        or root.parent != library_root
        or root.name.casefold() != "attachments"
    ):
        raise _native_failure(
            "The image attachment folder is not inside the validated copied snapshot.",
            operation="inspect_native_image_attachments",
        )
    _require_not_production(root, purpose="native image attachments")
    return root, stat_key(root_info)


def _ensure_root_unchanged(
    root: Path,
    expected: tuple[int, int, int, int, int, int],
    *,
    operation: str = "fingerprint_native_attachments",
) -> None:
    try:
        current = stat_key(root.lstat())
    except OSError as exc:
        raise _native_failure(
            "The copied attachment folder changed while it was inspected.",
            operation=operation,
        ) from exc
    if current != expected:
        raise _native_failure(
            "The copied attachment folder changed while it was inspected.",
            operation=operation,
        )


def _resolve_attachment_path(
    root: Path,
    raw_path: str,
    *,
    basename_map: dict[str, list[Path]] | None = None,
) -> tuple[Path | None, str, dict[str, list[Path]] | None]:
    """Resolve a copied binding with the library's unique-basename fallback."""

    def result(
        path: Path | None, state: str
    ) -> tuple[Path | None, str, dict[str, list[Path]] | None]:
        return path, state, basename_map

    try:
        stored = Path(raw_path)
    except (TypeError, ValueError, OSError):
        return result(None, "unavailable")
    try:
        if len(os.fsencode(raw_path)) > _MAX_STORED_PATH_BYTES:
            return result(None, "unsafe")
    except (UnicodeError, ValueError):
        return result(None, "unsafe")
    if stored.is_absolute():
        return _resolve_existing_attachment(root, stored, basename_map)
    if ".." in stored.parts:
        return result(None, "unsafe")

    original_parts = stored.parts
    relative_parts = original_parts
    if relative_parts and relative_parts[0].casefold() == "attachments":
        relative_parts = relative_parts[1:]
    if not relative_parts:
        return result(None, "missing")

    direct, direct_state, basename_map = _resolve_existing_attachment(
        root, root.joinpath(*relative_parts), basename_map
    )
    if direct is not None or direct_state != "missing":
        return direct, direct_state, basename_map
    if not stored.name:
        return result(None, "missing")

    if basename_map is None:
        try:
            basename_map = _attachment_paths_by_basename(root)
        except OSError:
            return result(None, "unavailable")
    matches = basename_map.get(stored.name, [])
    if len(matches) == 1:
        return result(matches[0], "available")
    return result(None, "ambiguous" if matches else "missing")


def _resolve_existing_attachment(
    root: Path,
    candidate: Path,
    basename_map: dict[str, list[Path]] | None,
) -> tuple[Path | None, str, dict[str, list[Path]] | None]:
    """Resolve a regular file without traversing a symlink in the copied tree."""
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None, "unsafe", basename_map
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None, "unsafe", basename_map

    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None, "missing", basename_map
        except ValueError:
            return None, "unsafe", basename_map
        except OSError:
            return None, "unavailable", basename_map
        if stat.S_ISLNK(info.st_mode):
            return None, "unsafe", basename_map
        is_last = index == len(relative.parts) - 1
        if is_last:
            if not stat.S_ISREG(info.st_mode):
                return None, "unsafe", basename_map
        elif not stat.S_ISDIR(info.st_mode):
            return None, "unsafe", basename_map
    if not relative.parts:
        return None, "unsafe", basename_map
    return current, "available", basename_map


def _attachment_paths_by_basename(root: Path) -> dict[str, list[Path]]:
    paths: dict[str, list[Path]] = {}
    scanned = 0

    def fail_walk(exc: OSError) -> None:
        raise exc

    for parent, directories, files in os.walk(root, followlinks=False, onerror=fail_walk):
        scanned += len(directories) + len(files)
        if scanned > _MAX_ATTACHMENT_SCAN_ENTRIES:
            raise OSError("The copied attachment directory exceeds the safe path scan limit")
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in files:
            candidate = Path(parent) / name
            resolved, state, _ = _resolve_existing_attachment(root, candidate, None)
            if state == "available" and resolved is not None:
                paths.setdefault(candidate.name, []).append(resolved)
    return paths


def _fingerprint_path(
    root: Path,
    raw_path: str,
    *,
    basename_map: dict[str, list[Path]] | None,
) -> tuple[str, tuple[int, int, int, int, int, int] | None, dict[str, list[Path]] | None]:
    first, state, basename_map = _resolve_attachment_path(root, raw_path, basename_map=basename_map)
    if first is None:
        second, second_state, basename_map = _resolve_attachment_path(
            root, raw_path, basename_map=basename_map
        )
        if second is not None or second_state != state:
            raise _native_failure(
                "An attachment path changed while the native fingerprint was collected.",
                operation="fingerprint_native_attachments",
            )
        return state, None, basename_map
    try:
        before = stat_key(first.stat())
    except OSError:
        return "unavailable", None, basename_map
    second, second_state, basename_map = _resolve_attachment_path(
        root, raw_path, basename_map=basename_map
    )
    if second_state != "available" or second != first:
        raise _native_failure(
            "An attachment path changed while the native fingerprint was collected.",
            operation="fingerprint_native_attachments",
        )
    try:
        after = stat_key(second.stat())
    except OSError as exc:
        raise _native_failure(
            "An attachment changed while the native fingerprint was collected.",
            operation="fingerprint_native_attachments",
        ) from exc
    if before != after:
        raise _native_failure(
            "An attachment changed while the native fingerprint was collected.",
            operation="fingerprint_native_attachments",
        )
    return "available", before, basename_map


def _read_stable_image_file(root: Path, path: Path) -> tuple[bytes, tuple[int, ...]]:
    try:
        resolved_before = resolve_confined_file(root, path)
        if resolved_before is None or resolved_before != path:
            raise GoodnotesError("The copied image path is unavailable.")
        before = stat_key(path.lstat())
        if not stat.S_ISREG(before[2]):
            raise GoodnotesError("The copied image is not a regular file.")
        if before[3] <= 0 or before[3] > _MAX_IMAGE_BYTES:
            raise GoodnotesError("The copied image exceeds the compressed image size limit.")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except (GoodnotesError, OSError, RuntimeError, ValueError) as exc:
        raise _native_failure(
            "The copied image is missing, unsafe, or exceeds its size limit.",
            operation="read_native_image",
        ) from exc

    try:
        opened_signature = stat_key(os.fstat(descriptor))
        if opened_signature != before or not stat.S_ISREG(opened_signature[2]):
            raise GoodnotesError("The copied image changed before it could be read.")
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_IMAGE_BYTES:
            chunk = os.read(
                descriptor,
                min(_IMAGE_READ_CHUNK, _MAX_IMAGE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) != before[3] or len(data) > _MAX_IMAGE_BYTES:
            raise GoodnotesError("The copied image changed or exceeds its size limit.")
        if stat_key(os.fstat(descriptor)) != before or stat_key(path.lstat()) != before:
            raise GoodnotesError("The copied image changed while it was read.")
        resolved_after = resolve_confined_file(root, path)
        if resolved_after != path:
            raise GoodnotesError("The copied image path changed while it was read.")
        return data, before
    except (GoodnotesError, OSError, RuntimeError, ValueError) as exc:
        raise _native_failure(
            "The copied image changed or could not be read safely.",
            operation="read_native_image",
        ) from exc
    finally:
        os.close(descriptor)


def _metadata_digest(values: tuple[object, ...]) -> str:
    def safe_value(value: object) -> object:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, bytes):
            return ("blob", len(value), hashlib.sha256(value).hexdigest())
        return (type(value).__name__, str(value))

    encoded = json.dumps(
        tuple(safe_value(value) for value in values),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
