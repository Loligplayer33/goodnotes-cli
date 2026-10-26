# Robustness and failure behavior

Tests use generated databases, PDFs, and attachments. They do not require the Goodnotes app or a private Library.

## Sync changes and failures

| Scenario | Behavior |
| --- | --- |
| Source inventory matches active copy | Reuse the view without hashing every byte. `--force` bypasses reuse. |
| Source changes during capture | Retry up to three times. If no stable copy succeeds, fail and preserve the prior active pointer. |
| Final inventory differs from initial inventory | Publish the validated view with `source_changed_during_refresh: true`, `captured_at`, and a warning. The next sync captures again. |
| Final inventory matches | Set `source_changed_during_refresh: false`. This reports matching checks, not source stability through command return. |
| Copied SQLite WAL needs consolidation | Write only to an independent staging copy, then validate before publication. |
| Ctrl-C during capture | Remove partial staging and preserve the active pointer. If killed before cleanup, the next sync removes abandoned staging. |

`sync` does not lock or control Goodnotes and does not open a source database engine. A continuously changing source may exhaust the three attempts.

| Test area | Coverage |
| --- | --- |
| [test_cache.py](../tests/test_cache.py), [test_cli.py](../tests/test_cli.py) | Reuse, cache damage, force refresh, source-change reporting, failure preserves pointer, and concurrency. |
| [test_snapshot.py](../tests/test_snapshot.py) | Clone fallback, WAL consolidation, concurrent source writes, and source-change retry. |
| [test_cache.py](../tests/test_cache.py) | Ctrl-C and abandoned staging. Tests simulate interruption; they do not send operating-system SIGKILL. |

These tests use synthetic sources, not a full Goodnotes app sync. Clone-specific tests skip where the host filesystem lacks clone support.

## Automatic refresh

`watch` does an initial refresh, polls metadata, coalesces bursts, and refreshes serially. It backs off after expected errors, preserves the previous snapshot, and reports recovery. It exits 1 after an unrecovered bounded run; Ctrl-C exits 130. It observes Mac files and cannot detect whether an iPad edit has arrived.

[test_watch.py](../tests/test_watch.py) covers native-only changes, bursts, continuous edits, transient failure, recovery, backoff, and interruption.

## Native reconstruction

| Invariant | Behavior |
| --- | --- |
| Engine access | Open RocksDB only on disposable scratch copied from a validated snapshot. Fresh options keep writes in scratch; never load copied `OPTIONS` files. |
| Cleanup | Remove scratch after extraction. Unsafe scratch paths, symlinks, special files, changing copies, and excessive decoding fail with diagnostics. |
| Supported records | Observed pen/highlighter records, filled paths, one outlined ellipse form, variable-width pencil, one two-point open shape, and some linked PNG/JPEG images. |
| Native images | Require an exact copied attachment binding and matching, untransformed placement rectangles. Missing body field 6 remains unknown, is counted as `native_image_body_field_6_absent_records`, and keeps coverage partial. |
| Unsupported data | Unknown versions, visibility, transforms, active types, or decoding reduce coverage. Fail if no supported object remains. Never substitute background-only output for missing native data. |
| Fidelity | Pencil texture is approximate. Generated tests do not establish full Goodnotes format support or rendering fidelity. |

| Tests | Evidence |
| --- | --- |
| [test_annotations.py](../tests/test_annotations.py), [test_decoders.py](../tests/test_decoders.py) | Observed record decoding, malformed inputs, and bounded decoding. |
| [test_native.py](../tests/test_native.py), [test_native_assets.py](../tests/test_native_assets.py), [test_native_rendering.py](../tests/test_native_rendering.py) | Scratch safety, attachment resolution, raster inputs, coverage, and geometry. |
| [test_native_workflow.py](../tests/test_native_workflow.py) | Generated RocksDB writer, read-only behavior, refresh, watch, rendering, retention, pruning, and SIGINT. |

Native workflow tests require a compatible optional engine. macOS CI installs RocksDB and makes them mandatory. They do not establish fresh iPad acceptance.

## Readers, refreshes, and locks

| Resource | Rule |
| --- | --- |
| `.sync.lock` | Serializes refreshes. |
| `.view.lock` | Readers open the persistent file read-only and hold a shared lock from pointer resolution through the last database or attachment read. |
| Publish/prune | Sync captures without the view lock, then takes it exclusively to publish and prune. A long reader delays publication and cleanup, not capture. |
| Managed generations | Retain the active generation. Remove superseded generations and associated annotated-PDF registrations after readers release the lock. |
| Explicit managed `--sandbox` | Follows the same locking and retention rules. |
| Named `snapshot` | Caller-owned; sync does not prune it. |

Goodnotes itself takes neither lock. Readers reject missing, denied, symlinked, or non-regular lock files and never recreate them. A nonblocking open avoids a raced FIFO; identity is checked after acquisition. Do not remove a lock while another CLI process may hold it.

Cache errors include stable codes and `operation`, `path`, and `remedy` details. Use the same `--data-dir` or `GOODNOTES_DATA_DIR` during setup and reads. See [access](access.md#read-access-for-an-agent).

`doctor --access-only` reports cache, source, scratch, output, and optional runtime availability without creating files or opening databases. Write checks are hints. [test_diagnostics.py](../tests/test_diagnostics.py) and [test_cache.py](../tests/test_cache.py) cover access failures, unsafe locks, replacement races, and reader-versus-refresh behavior. Permission denial tests skip under root because root bypasses mode restrictions.

## Pagination and search

Paged browse results include the active generation as `snapshot`. Pass it back as `--expect-snapshot ID`. A changed generation returns `stale_snapshot`; restart pagination against the new view. `test_expected_snapshot_mismatch_raises_stale_snapshot_error` in [test_cache.py](../tests/test_cache.py) covers this check.

`search` treats input as a literal, case-insensitive substring and binds it as a value. SQL-looking input cannot alter the query. See `test_search_uses_literal_casefolded_terms_scope_and_source_labels` in [test_library.py](../tests/test_library.py).

## Incomplete indexes and PDF scans

| Input or command | Behavior |
| --- | --- |
| Projection database | Required. Sync validates the reader-facing schema. |
| Text index | Optional. Missing search tables do not block navigation. `doctor`, `read`, and `search` report unavailable text sources or page mappings. |
| Stored-index search | Reports its coverage. Missing sources are not presented as a complete search with zero results. |
| `read` | Extracts from a mapped PDF background in the copy if indexed PDF text is absent. |
| `scan-pdfs` | Searches copied PDF backgrounds independently of the index. Default is 200 pages, maximum 250 per call. `next_page_offset` counts pages scanned, not hits. It reports missing, unmapped, unreadable, or truncated backgrounds and does not search handwriting. |

[test_library.py](../tests/test_library.py) covers missing-index behavior, coverage warnings, bounded scans, and fallback text budgets.

## Selectors and paths

Notebook selectors use exact ID, exact full path, or a unique name. Ambiguous selectors fail. A stale attachment prefix resolves by basename only when that basename is unique. Traversal, symlink escapes, and ambiguous names do not resolve.

Containment uses filesystem identity for path aliases, including aliases at different depths. Distinct names stay distinct on case-sensitive volumes. Cleanup uses directory descriptors, checks parents before deletion, and unlinks symlinks without changing targets. Malformed required catalog values fail; optional unknown geometry stays unknown.

See [test_library.py](../tests/test_library.py) and [test_safety.py](../tests/test_safety.py) for selectors, aliases, and cleanup cases.

## Registered annotated PDFs

`attach-pdf` checks readability and current notebook page count. Registration stores a sidecar path outside the immutable view and does not copy the PDF. Before annotated render/export, the CLI checks page count and content signature again. A moved, changed, or wrong-count file fails; output never falls back to background-only.

Registration cannot verify page order or annotation inclusion. Background reports `includes_handwriting: false`. Annotated output reports `includes_handwriting: null` and `annotation_status: user-provided-unverified`. See [test_documents.py](../tests/test_documents.py).

## Limits and output conflicts

| Command | Default or maximum |
| --- | --- |
| List/tree | 100 items by default. |
| `read` | At most 20 pages; 12,000 combined characters by default. |
| `search` | 20 snippets by default. |
| `bundle` | At most 10 pages; one 12,000-character budget across page text. |

`read` and `bundle` mark truncated text. Render, export, and bundle destinations must be outside the snapshot and production Goodnotes locations. The CLI never overwrites existing paths. Bundle files are staged into a newly reserved directory; the manifest is published last. A racing creator causes failure.

A multi-page native bundle may return `status: "partial"` after a page-local mapping, decoding, or rendering error if another image succeeds. It preserves page order and requested text.

Failed rows contain `image: null` and `render_error`. They have no success metadata or background substitute. The CLI retries failed pages on a later request.

If all images fail, or a single-page request fails, the CLI returns the original error and publishes no bundle. Runtime, safety, integrity, permission, cache, publication, and unexpected errors remain fatal.

Artifact bounds, leases, and ownership are documented in [architecture](architecture.md#generated-artifact-storage) and [output](output.md#temporary-and-saved-files). See [test_agent_output.py](../tests/test_agent_output.py), [test_artifacts.py](../tests/test_artifacts.py), and [test_storage_workflow.py](../tests/test_storage_workflow.py).

## Structured errors

| Failure | Response |
| --- | --- |
| Expected operation error | Compact JSON on stderr, exit code 1, no Python traceback. |
| Invalid argument | Structured error, exit code 2. |
| Closed output pipe | Interruption exit code 130. |
| SQLite/filesystem/output failure | Preserve operation context; filesystem errors include path and remedy. |

See error cases in [test_cli.py](../tests/test_cli.py).
