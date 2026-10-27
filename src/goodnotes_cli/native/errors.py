"""Native data failures and recovery guidance."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..errors import GoodnotesError


class NativeError(GoodnotesError):
    """Expected native-data failures with a stable recovery contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        path: Path | str = "",
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        if code == "native_runtime_unavailable":
            remedy = (
                "Install a current RocksDB shared library, for example 'brew install rocksdb' "
                "on macOS, or set GOODNOTES_ROCKSDB_LIBRARY to its absolute path. "
                "The library and its compression dependencies must be loadable by this process."
            )
        elif operation == "create_native_scratch":
            remedy = (
                "Grant write/search access to the temporary directory or set "
                "GOODNOTES_SCRATCH_DIR to a writable private directory outside the snapshot."
            )
        elif operation == "map_native_page":
            remedy = (
                "The copied page mapping or geometry is unsupported. Inspect this page's "
                "details, or use a Goodnotes-exported annotated PDF. Refreshing an unchanged "
                "copy does not add renderer support."
            )
        elif operation in {"decode_native_page", "render_native_page"}:
            remedy = (
                "The copied page's native records could not be reconstructed safely. Inspect "
                "the reported coverage, or use a Goodnotes-exported annotated PDF. Refreshing "
                "an unchanged copy does not add renderer support."
            )
        elif operation == "inspect_native_options":
            remedy = (
                "The copied native store uses unsupported or invalid engine options. Inspect "
                "the reported compatibility details; refreshing an unchanged copy does not "
                "add native format support."
            )
        else:
            remedy = (
                "The copied native store could not be read reliably. Inspect the reported "
                "operation and path. If the copied data is missing or damaged, refresh it "
                "with sync; otherwise resolve the reported native-store compatibility or "
                "read failure before evaluating annotations."
            )
        self.details: dict[str, object] = {
            "operation": operation,
            "path": str(path),
            "remedy": remedy,
        }
        if context is not None:
            self.details.update(context)
