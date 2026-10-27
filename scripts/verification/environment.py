"""Construct isolated subprocess environments for verification tasks."""

from __future__ import annotations

import os
from collections.abc import Mapping

_PRESERVED_GOODNOTES_SETTINGS = {"GOODNOTES_ROCKSDB_LIBRARY"}


def isolated_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Remove inherited harness configuration, then apply explicit child settings.

    The selected RocksDB runtime is intentionally preserved so native checks can
    run in the same environment as the user's prepared toolchain. Goodnotes target
    settings, Python import/runtime overrides, and pytest startup controls are set
    by the harness or omitted to keep the selected suite deterministic.
    """
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GOODNOTES_") and key not in _PRESERVED_GOODNOTES_SETTINGS:
            environment.pop(key)
        elif key.startswith(("PYTHON", "PYTEST_")):
            environment.pop(key)
    if overrides:
        environment.update(overrides)
    return environment
