# Robustness and failure behavior

This page maps common failure cases to the CLI's response and the synthetic tests that cover them. The tests use generated databases, PDFs, and attachments. They do not require the Goodnotes app or a private library.

## Goodnotes changes during sync

`sync` never locks or controls Goodnotes and never opens a source database engine. It inventories databases, sidecars, optional native store files, and attachments. Unchanged source and cache inventories reuse the active view. During a changed refresh, unchanged files reuse prior verified digests only after matching both fingerprints. Changed files are captured and checked again. SQLite reuse requires an unchanged complete source family. `--force` bypasses reuse. APFS clones or fallback byte copies always use independent inodes. Copied SQLite WAL consolidation occurs before validation and publication.

Changes detected during an individual capture attempt cause a retry. If no attempt gets a stable source copy within three tries, sync fails and keeps the previous active pointer. After capture, sync compares the source inventory taken before capture with a final inventory. The comparison includes any earlier attempts and runs before sync waits for the exclusive view lock to publish. If the inventories differ, sync publishes the validated view and sets `source_changed_during_refresh: true`, with `captured_at` and a warning that the view is valid as of that time. If they match, the flag is false. That result means only that the two inventories matched. It does not prove the source stayed unchanged through command return. Updates after the final check, including while sync waits to publish, are detected by the next sync. A mismatch omits the reusable source fingerprint, so the next sync captures a fresh view. `captured_at` is the validated view's capture time.

Generated tests cover inventory reuse, cache metadata damage, forced refresh, source-change reporting, WAL and attachment changes, independent cloning, clone fallback, copied-WAL consolidation, active WAL writes without source locking, and retries after source changes. See `test_refresh_skips_capture_when_source_inventory_is_unchanged`, `test_same_size_cached_attachment_replacement_triggers_refresh`, `test_source_change_during_refresh_is_reported_with_snapshot_time`, `test_force_refreshes_an_unchanged_source`, and `test_failed_force_refresh_preserves_the_active_pointer` in [test_cache.py](../tests/test_cache.py), plus `test_sync_reuses_unchanged_and_force_rebuilds` in [test_cli.py](../tests/test_cli.py), and `test_real_cow_clone_has_independent_writes`, `test_snapshot_falls_back_to_copy_after_exdev_and_keeps_source_unchanged`, `test_snapshot_consolidates_copied_wal_and_reads_committed_rows`, `test_snapshot_fails_during_active_wal_writes_without_locking_source`, and `test_snapshot_retries_after_a_source_change` in [test_snapshot.py](../tests/test_snapshot.py). These tests use synthetic sources and writers. They do not run a full Goodnotes app sync. The macOS clone test skips on platforms or filesystems without clone support.

This remains a file-copy workflow, not a transaction coordinated with the Goodnotes app. A continuously changing source can make sync fail after three attempts.

WAL consolidation checkpoints only an independent staging copy. The copied SHM index is discarded before opening that copy so SQLite can rebuild its index and checkpoint state. An incomplete or failed checkpoint fails capture and preserves the previous active view. A genuine hot rollback journal is recovered only on the independent staging copy before backup. Fallback output uses DELETE journal mode. Source files and completed snapshots are never opened for SQLite writes. SQLite determines the valid committed WAL prefix; successful consolidation and integrity checks do not prove that an invalid or torn tail contained no additional edits. This limitation also applies to the backup path.

Generated snapshot tests cover stale SHM reconstruction, owner-read-only source files, incomplete checkpoints, failed journal-mode changes, nonempty leftover sidecars, rollback-journal fallback, stray WALs on non-WAL databases, valid commits before torn or reset WAL tails, and FTS databases whose custom tokenizer is unavailable. Source byte hashes and metadata stay unchanged; failed capture removes unpublished output. See [test_snapshot.py](../tests/test_snapshot.py).

Incremental regression tests cover one changed WAL alongside a large unchanged SST, native CURRENT/manifest rollover and deletion, tampered cached bytes, mutation after reuse planning, and SQLite-family changes. Capture statistics report successful-attempt logical bytes, not physical allocation or work spent in failed attempts. See [snapshot tests](../tests/test_snapshot.py).

## Automatic refresh

`watch` performs an initial refresh, then polls source metadata. It coalesces bursts, refreshes serially, and stays quiet when unchanged. Expected failures emit error events, preserve the previous snapshot, and back off before retrying. Recovery emits an event. A bounded watcher ending with an unrecovered error exits 1; Ctrl-C exits 130. It observes files on the Mac, without knowing whether a new iPad edit has arrived. Generated tests cover native-only changes, burst coalescing, continuous edits, transient failures, recovery, backoff, and interruption in [watch tests](../tests/test_watch.py).

## Native reconstruction

Native rendering opens the engine only on disposable scratch cloned from a validated snapshot. It never opens the source or completed snapshot with RocksDB. Fresh options place engine writes inside scratch; copied OPTIONS files are never loaded. Scratch is removed after extraction. Symlinks, special files, unstable copies, excessive decoding, and unsafe scratch locations fail with diagnostics.

Native reconstruction supports observed pen and highlighter schemas, filled paths, one outlined ellipse form, one variable-width pencil format, and some linked PNG/JPEG images. Pencil texture is approximate. Image files must resolve through the exact copied attachment binding, and the stored top and center rectangles must match; images must be unrotated and uncropped. Rotated or cropped page backgrounds and unsupported coordinate mappings remain unavailable. The renderer counts filled paths as strokes; native images are reported separately and do not count as handwriting. Unsupported active types, uncertain visibility, ambiguous versions, unmodeled fields, parent transforms, and decoding failures reduce coverage or fail when no supported object remains. Rendering retains its approximation warning even when every observed record decodes. Missing native roots mean unavailable data, not an empty page. See [native safety tests](../tests/test_native.py) and [decoder tests](../tests/test_annotations.py).

Ink with a single start retains its ordered point stream. Differences between adjacent stored coordinates do not establish a pen lift. Start-only records render separate dots; records that mix multiple starts and coordinate pairs remain unsupported because their command boundaries have not been established. Decoder tests cover these distinctions, long segments, retracing, dash validation, and bounded decompression. [Low-level decoder tests](../tests/test_decoders.py) cover LZ4 back-references, overlapping matches and cross-block history, plus TPL field widths and string lengths.

[Image attachment tests](../tests/test_native_assets.py) cover exact document bindings, unsafe paths, changing inputs, byte and pixel limits, malformed PNG scanlines, and truncated JPEG structures. JPEG checks do not prove every scan bitstream is intact; decoder recovery remains an explicit integrity warning. [Rendering tests](../tests/test_native_rendering.py) verify that integrity status survives page rendering, image budgets retain truthful counts, and paths and variable-width strokes produce pixels. Filled arcs with unsupported start discontinuities fail rather than changing their encoded geometry.

The [native workflow tests](../tests/test_native_workflow.py) create their own writable RocksDB store and leave its writer open. They verify native WAL updates, lasso moves, whole-object erasure, watch-driven refresh, PNG/PDF/bundle consistency, read-only cache access, source and snapshot preservation, reader retention, pruning, and SIGINT. These tests require an optional compatible engine. macOS CI installs it and makes these tests mandatory. Generated tests do not establish fidelity to Goodnotes or fresh iPad acceptance.

## Readers and refreshes

`.sync.lock` serializes full refreshes. A reader acquires `.view.lock` in shared mode before resolving the active pointer and keeps it through its last database or attachment read. Sync copies changed source files without holding `.view.lock`, then takes that lock exclusively to publish the new pointer and prune superseded managed generations. A long reader delays publication and pruning, not the source capture. The Goodnotes app does not take either lock.

The managed cache retains the active generation. Once existing readers release the view lock, a successful refresh removes older managed generations and their annotated-PDF registrations. An explicit `--sandbox` path inside this cache follows the same lock and lifetime rules. The separate `snapshot LIBRARY DESTINATION` command creates a caller-owned copy. Keep its destination outside the managed cache; normal sync does not remove it.

Tests cover unchanged reuse, one active generation after refresh, old-reader retention while capture proceeds, serialized concurrent syncs, and explicit managed-sandbox locking. See `test_refresh_retains_only_active_generated_snapshot_and_removes_registration`, `test_old_reader_holds_snapshot_while_capture_progresses_and_publication_waits`, `test_concurrent_refreshes_serialize_and_second_sees_fresh_cache`, and `test_explicit_managed_sandbox_takes_shared_view_lock` in [test_cache.py](../tests/test_cache.py). `test_render_loads_read_view_only_once` in [test_cli.py](../tests/test_cli.py) checks that one command resolves the read view once.

Immutable schema, page and search-binding caches live only inside that read view. Generated query-count tests bound repeated hydration across a 500-page notebook and multiple reads. PDF scans reuse bounded document handles. Multipage native and annotated operations share their engine or verified PDF within the command; tests check open/hash counts, failure cleanup, pixels, selected-page order and resource sharing in [Library tests](../tests/test_library.py) and [document tests](../tests/test_documents.py).

## Read-only cache access

Sync creates persistent lock files in the CLI cache. A reader opens the existing view lock read-only, acquires a shared lock, and creates no cache files or directories. Read access to the cache is sufficient for browsing, searching, and reading. Export commands also need write access to their output destination; registering an annotated PDF writes separate registration metadata.

Readers reject missing locks, denied access, symlinks, and non-regular lock files. A nonblocking open prevents a raced FIFO from hanging before validation. The opened file's identity is checked again after the shared lock is acquired. Missing locks are never recreated by readers. Stop other CLI processes before using sync to repair a missing lock; deleting a lock while it is held breaks coordination through its original inode. The CLI never removes its lock files during normal operation.

Cache access errors include a stable code and `details` with `operation`, `path`, and `remedy`. `cache_not_initialized` means setup or a required lock is missing, `cache_permission_denied` means the runner lacks access, and `cache_invalid` means the selected cache state failed validation. The CLI does not bypass locking or choose a different cache. Use the same `--data-dir` or `GOODNOTES_DATA_DIR` setting for setup and reading. See [agent setup](usage.md#read-access-for-an-agent).

`doctor --access-only` works without an initialized cache and reports selected cache or sandbox, optional source, scratch, and output access. Write checks are labelled hints and create nothing. Native runtime availability is separate. Source and output permission failures retain operation/path/remedy, and database contexts preserve caller output errors. Annotated-PDF lookup creates no sidecar directories. See [diagnostic tests](../tests/test_diagnostics.py).

Generated tests exercise cache and source bytes and metadata before and after read-only commands, denied pointer and database access, malformed catalogs, missing-lock repair, unsafe lock files, replacement during acquisition, and interruption. The existing reader-versus-refresh tests also run through the read-only shared lock and ensure publication and pruning wait for readers. See [cache tests](../tests/test_cache.py) and [installed CLI tests](../tests/test_cli.py). Permission-mode assertions run as an ordinary user on macOS and Linux; root bypasses those permissions, so those denial cases skip when tests run as root.

## Interrupted refreshes and failed validation

Refreshes capture into a staging directory and publish only after full validation. Ctrl-C removes the partial staging directory and leaves the prior active pointer unchanged. If the process is killed before cleanup, the next sync removes abandoned staging directories. A failed capture also preserves the previous active view. The tests simulate an interrupted capture and an abandoned staging directory; they do not send an operating-system SIGKILL to the process.

See `test_interrupted_capture_cleans_partial_staging_and_preserves_pointer` and `test_abandoned_staging_is_cleaned_on_next_refresh` in [test_cache.py](../tests/test_cache.py), `test_snapshot_rejects_projection_missing_core_schema` in [test_snapshot.py](../tests/test_snapshot.py), and `test_failed_refresh_preserves_active_view` in [test_cli.py](../tests/test_cli.py).

## Paginating across a refresh

Paged command results include the active generation ID in `snapshot`. A client that needs one consistent sequence passes that ID back with the global `--expect-snapshot ID` option. If sync has changed the active generation, the CLI returns a structured error with code `stale_snapshot`. The client can restart pagination using the new generation instead of mixing pages from different copies.

`test_expected_snapshot_mismatch_raises_stale_snapshot_error` in [test_cache.py](../tests/test_cache.py) covers the stale-generation check. `test_listing_paginates_and_projects_fields` and `test_pdf_scan_has_page_cursor` in [test_cli.py](../tests/test_cli.py) cover result cursors.

## Literal search terms

`search` treats a query as a literal, case-insensitive substring. SQL-looking input is bound as a value and cannot change the query. `test_search_uses_literal_casefolded_terms_scope_and_source_labels` in [test_library.py](../tests/test_library.py) covers literal matching and source labels.

## Incomplete text indexes and PDF scans

The projection database is required and sync validates its reader-facing schema. The text index is optional, so missing search tables do not block browsing. `doctor`, `read`, and `search` report unavailable text sources or page mappings. Stored-index search identifies its coverage and does not treat a missing source as a complete search with no results.

`read` can extract text from a mapped PDF background in the copied library when indexed PDF text is absent. `scan-pdfs` searches copied PDF backgrounds independently of the stored index. It scans at most 200 pages by default and 250 per call. Its `next_page_offset` counts pages scanned, not hits. It reports missing, unmapped, unreadable, or truncated backgrounds and does not search handwriting.

See `test_missing_search_index_does_not_block_navigation`, `test_missing_text_sources_are_reported_and_explicit_search_fails`, and `test_missing_page_metadata_warns_about_stored_index_coverage` in [test_library.py](../tests/test_library.py), along with `test_pdf_scan_is_bounded_paginated_and_independent_of_the_search_index` and `test_pdf_fallback_respects_read_budget_and_reports_truncation` in [test_library.py](../tests/test_library.py).

## Selectors and attachment paths

The CLI resolves a notebook by exact ID, exact full path, or unique name. An ambiguous selector returns an error instead of selecting a possible match. A stale attachment prefix resolves by basename only when that name appears once in the copied attachments. Parent-directory traversal, symlink escapes, and ambiguous filenames do not resolve.

See `test_basename_ambiguity_and_orphaned_items_are_not_guessed`, `test_full_path_collisions_and_kind_mismatches_are_reported`, and `test_stale_attachment_prefix_is_not_remapped_when_basename_is_ambiguous` in [test_library.py](../tests/test_library.py), plus path and symlink checks in [test_safety.py](../tests/test_safety.py).

Containment uses filesystem identity for aliases, including aliases with different path depths, without folding distinct names on case-sensitive volumes. Cleanup uses directory descriptors and rejects replaced parents before deletion. It unlinks symlinks without changing their targets. Generated tests cover alias identities, missing output leaves, symlink targets and replacement races. Volume-specific tests exercise actual case behavior when the host supports it. Malformed required catalog values fail with explicit diagnostics; optional unknown geometry remains unknown.

## Registered annotated PDFs

`attach-pdf` checks that a PDF is readable and has the notebook's current page count. The CLI stores registration metadata outside the immutable read view. Before rendering or exporting the `annotated` layer, it checks the page count and PDF content signature again. If the PDF moves, changes, or no longer matches the current notebook page count, the operation fails instead of falling back to background-only output.

These checks cannot verify the PDF's page order or whether annotation export was enabled. Background output reports `includes_handwriting: false`. Annotated output reports `includes_handwriting: null` and `annotation_status: user-provided-unverified`.

See `test_register_rejects_pdf_page_count_mismatch`, `test_registered_pdf_must_match_current_notebook_page_count`, `test_registered_pdf_integrity_detects_same_size_edit_with_restored_mtime`, and `test_annotated_layer_never_falls_back_to_background` in [test_documents.py](../tests/test_documents.py).

## Limits and output conflicts

List and tree results return 100 items by default. `read` accepts at most 20 pages and limits combined text to 12,000 characters by default. `search` returns at most 20 short snippets. `bundle` accepts at most 10 pages and applies one 12,000-character default budget across their text. `read` and `bundle` set `truncated` when the budget cuts text.

Render, export, and bundle output paths must be outside the snapshot and production Goodnotes locations. Existing files are not overwritten. A bundle is prepared in a temporary directory, then its image files and manifest are published into a newly reserved output directory. The manifest is published last. If another process creates the destination first, the CLI fails rather than replacing it.

With no explicit `--output`, render/bundle artifacts use a separately marked, locked cache with a 100,000,000-byte retained-payload cap and 24-hour idle expiry. PNG payloads are deduplicated through internal hardlinks; snapshot/source files are never linked. A five-minute lease protects returned paths from TTL and capacity eviction. Expiry and capacity cleanup run on the next managed artifact operation, leaving caller-saved outputs and immutable read views untouched. Entry publication stages complete files before exposing a result. Before and after cache reuse, the renderer checks source, native-store, and native-image attachment metadata; a cache hit preserves native coverage and fidelity warnings. Renderer version changes invalidate old page renders. A bundle always reads current source-labelled text.

Generated [artifact tests](../tests/test_artifacts.py) cover byte accounting, expiry, grace periods, deduplication, concurrent publication, ownership, and cleanup boundaries. [Storage workflows](../tests/test_storage_workflow.py) cover render reuse, fresh text, cross-view deduplication, persistent exports, and native edits invalidating image reuse. [Artifact diagnostics](../tests/test_artifact_diagnostics.py) cover optional write hints without filesystem changes. These tests do not access a live Goodnotes library.

Concise responses preserve interpretation limits, with full metadata available through `--verbose` or explicit `--fields` projection. Bundle returns the same bounded text inline and in its full on-disk manifest. Generated output tests verify source labels, explicit nulls, warnings, coverage, page order, budgets, pagination, and invalid source rejection before side effects. Native workflow tests exercise default study bundles through the real optional engine with generated records. See [test_agent_output.py](../tests/test_agent_output.py) and [test_native_workflow.py](../tests/test_native_workflow.py).

See `test_read_reports_budget_and_preserves_handwriting`, `test_bundle_budget_and_published_paths`, and `test_bundle_publisher_does_not_replace_racing_directory` in [test_cli.py](../tests/test_cli.py), plus `test_output_file_is_never_overwritten` and output-path checks in [test_documents.py](../tests/test_documents.py) and [test_safety.py](../tests/test_safety.py).

## Structured errors

Expected operation errors return compact JSON on stderr with exit code 1. Invalid arguments return a structured error with exit code 2. These errors do not print a Python traceback. See `test_missing_library_is_structured_stderr` and `test_argument_errors_are_structured` in [test_cli.py](../tests/test_cli.py).

Query-time SQLite failures retain the cache operation, path and remedy. Output failures retain their output context. A closed output pipe follows the existing interruption exit code 130 without a generic operation error. Doctor field projections are validated before accessing data, including its separate `--access-only` shape. Unicode casefold expansion is mapped back to original text before constructing search snippets.

[Generated CLI audit workflows](../tests/test_audit_workflow.py) exercise capture followed by navigation and Unicode search, missing-PDF scan/render diagnostics, and disconnected native dots. They verify source and completed-snapshot bytes and metadata remain unchanged by readers. Native writer, refresh, read-only cache, watch and signal workflows remain in [native workflow tests](../tests/test_native_workflow.py).
