"""Compatibility exports for snapshot creation."""

from .storage.capture.copy import _clone_file_from_fd as _clone_file_from_fd
from .storage.capture.lifecycle import create_snapshot as create_snapshot
from .storage.capture.types import _InventoryChanged as _InventoryChanged
