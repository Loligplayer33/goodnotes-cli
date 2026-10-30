"""Foreground, serial refreshes of an isolated Goodnotes copy."""

from __future__ import annotations

import math
import time
from collections.abc import Generator
from pathlib import Path

from .errors import FilesystemAccessError, GoodnotesError, _permission_denied
from .storage.cache.lifecycle import refresh_cache
from .storage.capture.types import _InventoryChanged
from .storage.inventory import _input_signature


def _validate_options(interval: float, debounce: float, iterations: int | None) -> None:
    if isinstance(interval, bool) or not math.isfinite(interval) or not 0.05 <= interval <= 3600:
        raise GoodnotesError("Watch interval must be between 0.05 and 3600 seconds.")
    if isinstance(debounce, bool) or not math.isfinite(debounce) or not 0 <= debounce <= 60:
        raise GoodnotesError("Watch debounce must be between 0 and 60 seconds.")
    if iterations is not None and (
        isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1
    ):
        raise GoodnotesError("Watch iterations must be a positive integer when provided.")


def _error_event(exc: GoodnotesError | OSError, iteration: int, retry_delay: float) -> dict:
    error: dict[str, object] = {
        "code": getattr(exc, "code", "operation_failed"),
        "message": str(exc),
    }
    details = getattr(exc, "details", None)
    if details is not None:
        error["details"] = details
    return {
        "event": "error",
        "iteration": iteration,
        "error": error,
        "retry_in_seconds": retry_delay,
    }


def _source_signature(source: Path) -> str:
    try:
        return _input_signature(source)
    except (GoodnotesError, OSError) as exc:
        if not _permission_denied(exc):
            raise
        raise FilesystemAccessError.from_exception(
            exc,
            operation="inspect_source_library",
            path=source,
            kind="source",
        ) from exc


def watch_library(
    library: Path,
    data_dir: Path,
    *,
    interval: float = 2,
    debounce: float = 1,
    iterations: int | None = None,
) -> Generator[dict[str, object], None, None]:
    """Refresh immediately, then coalesce source metadata changes before refreshing.

    Each iteration is one source inventory check. Unchanged checks produce no event
    after the first successful refresh. A burst must settle for ``debounce`` seconds,
    with a maximum coalescing delay of at least ten seconds so ongoing edits cannot
    defer captures indefinitely. Refresh failures leave retention and recovery to
    ``refresh_cache`` and back off before retrying. Interrupts propagate to the caller.

    This observes files that have reached the Mac. It does not establish whether an
    iPad's latest edit has arrived. It never opens a source database engine.
    """

    _validate_options(interval, debounce, iterations)
    source = Path(library).expanduser()
    destination = Path(data_dir).expanduser()
    baseline: str | None = None
    candidate: str | None = None
    candidate_since = 0.0
    first_change = 0.0
    inventory_unsettled = False
    last_inventory_change: _InventoryChanged | None = None
    max_coalescing = max(10.0, debounce * 5)
    failures = 0
    emitted_ready = False
    last_snapshot: object = None
    delay = interval
    iteration = 0

    while iterations is None or iteration < iterations:
        if iteration:
            time.sleep(delay)
        iteration += 1
        try:
            # Remember the inventory BEFORE refresh. Taking it afterwards could
            # swallow an edit that arrived after the captured view was selected.
            signature: str | None
            now = time.monotonic()
            try:
                signature = _source_signature(source)
            except _InventoryChanged as exc:
                # One inventory pass can race a normal filesystem update. Treat
                # this as an unknown candidate, not as a failed refresh: retain
                # the original max-coalescing deadline and restart the quiet
                # window when a stable changed signature is observed. Permission,
                # malformed-state, and unsafe-entry errors take the regular
                # error/backoff path below.
                if not inventory_unsettled and (baseline is None or candidate is None):
                    first_change = now
                last_inventory_change = exc
                signature = None
                candidate = None
                inventory_unsettled = True
                # Do not let continuous metadata races defer a snapshot forever.
                # The capture path has its own consistency checks and will still
                # reject a source that cannot produce a validated read view.
                should_refresh = now - first_change >= max_coalescing
            else:
                should_refresh = baseline is None
                if baseline is not None and signature == baseline:
                    candidate = None
                    first_change = 0.0
                    inventory_unsettled = False
                    # A failed refresh or an initial cache error should be retried
                    # after backoff even if the source inventory has settled.
                    should_refresh = failures > 0
                elif baseline is not None:
                    if candidate is None:
                        if not inventory_unsettled:
                            first_change = now
                        inventory_unsettled = False
                    if candidate != signature:
                        candidate = signature
                        candidate_since = now
                    should_refresh = (
                        now - candidate_since >= debounce or now - first_change >= max_coalescing
                    )
            if not should_refresh:
                if candidate is None or inventory_unsettled:
                    delay = interval
                else:
                    remaining_debounce = max(0.0, candidate_since + debounce - now)
                    remaining_max = max(0.0, first_change + max_coalescing - now)
                    delay = min(interval, remaining_debounce, remaining_max)
                continue

            try:
                result = refresh_cache(source, destination)
            except _InventoryChanged as exc:
                # A capture that races the source is also unsettled metadata,
                # not a permission, corruption, or unsafe-entry failure. Start a
                # new bounded window so max-coalescing retries cannot spin.
                first_change = time.monotonic()
                candidate = None
                inventory_unsettled = True
                last_inventory_change = exc
                delay = interval
                continue

            recovered = failures > 0
            failures = 0
            delay = interval
            if result.get("source_changed_during_refresh") is True or signature is None:
                baseline = None
            else:
                baseline = signature
            candidate = None
            inventory_unsettled = False
            last_inventory_change = None
            snapshot = result.get("snapshot_id")
            if recovered:
                event = "recovered"
            elif not emitted_ready:
                event = "ready"
            elif snapshot != last_snapshot:
                event = "updated"
            else:
                event = None
            emitted_ready = True
            last_snapshot = snapshot
            if event is not None:
                yield {**result, "event": event, "iteration": iteration}
        except (GoodnotesError, OSError) as exc:
            failures += 1
            # At least one second between failures, at most thirty seconds unless
            # the caller deliberately selected a longer polling interval.
            delay = max(interval, min(30.0, 2.0 ** min(failures - 1, 5)))
            yield _error_event(exc, iteration, delay)

    if (
        iterations is not None
        and not emitted_ready
        and failures == 0
        and last_inventory_change is not None
    ):
        # Keep transient startup races quiet while the watch can still recover.
        # If its requested bound ends without one validated view, report that
        # concrete terminal condition so supervisors do not treat no work as success.
        yield _error_event(last_inventory_change, iteration, interval)
