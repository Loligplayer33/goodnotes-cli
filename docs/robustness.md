# Robustness and failure behavior

Tests use generated databases, PDFs, and attachments, not a live Goodnotes Library.

## Sync and readers

| Scenario | Result |
| --- | --- |
| Source changes during capture | Try at most three captures; if none succeeds, fail and keep the previous active pointer. |
| Final inventory differs during this refresh | Publish the validated copy with `source_changed_during_refresh: true` and `captured_at`; next sync captures again. `false` means no change was detected during this refresh, not that no edits happened since the prior sync. Matching inventories do not guarantee freshness at return. |
| Interrupt during capture | Ctrl-C removes staging and preserves the pointer. Next sync removes staging left by a killed process. |
| Reader overlaps refresh | Shared `.view.lock` retains its snapshot through the last read. Sync captures without it and waits for exclusive access to publish/prune. |

The app does not participate in locks. A changing source can exhaust retries. Tests simulate interruption, not OS SIGKILL; clone-specific tests skip when clone support is unavailable.

| Tests | Scenarios |
| --- | --- |
| [test_cache.py](../tests/storage/test_cache.py), [test_cli.py](../tests/interface/test_cli.py) | Inventory reuse, force refresh, changed-source reporting, pointer preservation, locks, and concurrency. |
| [test_snapshot.py](../tests/storage/test_snapshot.py) | Independent copies, clone fallback, SQLite WAL consolidation, and source changes. |
| [test_watch.py](../tests/storage/test_watch.py) | Burst coalescing, transient failures, recovery, backoff, and interruption. |

## Native reconstruction

RocksDB opens only on disposable scratch copied from a validated snapshot. Fresh options keep writes in scratch; copied `OPTIONS` files are not loaded. Scratch is removed after extraction, and byte-copy fallback is limited to 64 MiB per extraction.

Unknown visibility, geometry, transforms, versions, and records remain unknown or reduce coverage. A successful lookup that finds no records reports `native-no-records-observed` / `no-native-records-observed` and an unverified-absence warning; it does not prove a blank page. Missing stores, runtime failures, and read errors remain errors. Native rendering never silently falls back to the background. Pencil decoding preserves the configured width and relative sample dynamics. Rendered tone, stroke width, taper, contours, absolute pressure response and texture remain approximate. Faint gray details around some fill edges remain unverified. Generated tests do not establish full Goodnotes compatibility or rendering fidelity.

| Tests | Coverage |
| --- | --- |
| [test_annotations.py](../tests/native/test_annotations.py), [test_decoders.py](../tests/native/test_decoders.py) | Observed schemas, malformed input, and bounded decoding. |
| [test_native.py](../tests/native/test_native.py), [test_native_assets.py](../tests/native/test_native_assets.py), [test_native_rendering.py](../tests/rendering/test_native_rendering.py) | Scratch safety, copied image bindings, raster validation, and drawing. |
| [test_native_workflow.py](../tests/journeys/test_native_workflow.py) | Generated RocksDB updates, read-only behavior, watch, rendering, retention, pruning, and SIGINT. Requires optional engine; macOS CI makes it mandatory. |

## Readers and output boundaries

| Input or operation | Behavior |
| --- | --- |
| Text index and doctor counts | Optional. Missing tables do not block navigation; text commands report unavailable sources and coverage. Counts describe the selected copy and do not establish source-index completeness or data loss. |
| `search` | Literal, case-insensitive substring; SQL-looking input is bound as data. |
| Notebook selector | Exact ID, exact full path, or unique name. Ambiguity fails instead of guessing. |
| Attachment path | Basename remapping requires a unique name. Traversal and symlink escapes fail. |
| Registered annotated PDF | Recheck signature and page count before annotated output. Page order and annotation inclusion remain unverified; never fall back to background. |
| Bundle page-local error | Multi-page output may be partial if any image succeeds. Failed rows have `image: null` and `render_error`, without success metadata or a background substitute. If all images fail, publish nothing. |
| Full PDF export error | All-or-nothing. Any selected-page failure prevents publication of a partial PDF and keeps the page context in the error. |
| Output destination | Must be outside snapshot and production Library paths. Existing paths are never overwritten; bundle manifest publishes last. |

Filesystem containment uses identity checks for aliases. Cleanup checks parent directories and unlinks symlinks without changing targets. See [test_safety.py](../tests/storage/test_safety.py), [test_library.py](../tests/reader/test_library.py), [test_documents.py](../tests/rendering/test_documents.py), and [test_agent_output.py](../tests/interface/test_agent_output.py).

For cache bounds and leases, see [Architecture](architecture.md#artifact-cache) and [test_artifacts.py](../tests/storage/test_artifacts.py), [test_storage_workflow.py](../tests/journeys/test_storage_workflow.py).
