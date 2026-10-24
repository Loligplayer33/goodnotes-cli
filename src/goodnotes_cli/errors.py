"""Expected failures that the CLI presents without a traceback."""


class GoodnotesError(Exception):
    """A library, input, or capability error with a user-facing explanation."""


class StaleSnapshotError(GoodnotesError):
    """A continuation expects a different generation than the active read view."""

    code = "stale_snapshot"


class TextIndexUnavailableError(GoodnotesError):
    """The requested stored text index cannot answer this search request."""
