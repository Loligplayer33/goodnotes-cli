"""Public snapshot validation and safe path operations."""

from .storage.paths import (
    path_contains as path_contains,
)
from .storage.paths import (
    paths_overlap as paths_overlap,
)
from .storage.paths import (
    resolve_confined_file as resolve_confined_file,
)
from .storage.paths import (
    safe_output as safe_output,
)
from .storage.paths import (
    safe_remove_tree as safe_remove_tree,
)
from .storage.paths import (
    stat_key as stat_key,
)
from .storage.sandbox import (
    SNAPSHOT_MARKER as SNAPSHOT_MARKER,
)
from .storage.sandbox import (
    Sandbox as Sandbox,
)
from .storage.sandbox import (
    open_database as open_database,
)
