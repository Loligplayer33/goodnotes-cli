"""Shared structural types for document rendering."""

from pathlib import Path
from typing import Protocol

from ..storage.paths import OutputProtection


class SandboxProtocol(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...

    @property
    def attachments_dir(self) -> Path: ...

    @property
    def projection_path(self) -> Path: ...

    def ensure_output(self, path: Path) -> Path: ...

    def output_protection(self) -> OutputProtection: ...
