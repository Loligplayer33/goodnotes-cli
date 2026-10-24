# Robustness and failure behavior

This page maps common failure cases to the CLI's response and the synthetic tests that cover them. The tests use generated databases, PDFs, and attachments. They do not require the Goodnotes app or a private library.

## Goodnotes changes during sync

`sync` never locks or controls Goodnotes and never opens a source SQLite database. It builds a stat inventory of the required databases, sidecars, and attachment tree, then checks the active copy's saved stat inventory. It reuses the active view only when the source path and both inventories match. This detects ordinary damage to the cached tree without rehashing every file. `sync --force` bypasses the metadata shortcut and performs a new verified capture. On macOS the copier tries copy-on-write clones and falls back to ordinary independent copies when cloning is unavailable. It hashes and validates the copy, consolidates WAL data using only copied databases, and checks the source again.

Changes detected during an individual capture attempt cause a retry. If no attempt gets a stable source copy within three tries, sync fails and keeps the previous active pointer. After capture, sync compares the source inventory taken before capture with a final inventory. The comparison includes any earlier attempts and runs before sync waits for the exclusive view lock to publish. If the inventories differ, sync publishes the validated view and sets `source_changed_during_refresh: true`, with `captured_at` and a warning that the view is valid as of that time. If they match, the flag is false. That result means only that the two inventories matched. It does not prove the source stayed unchanged through command return. Updates after the final check, including while sync waits to publish, are detected by the next sync. A mismatch omits the reusable source fingerprint, so the next sync captures a fresh view. `captured_at` is the validated view's capture time.

Generated tests cover inventory reuse, cache metadata damage, forced refresh, source-change reporting, WAL and attachment changes, independent cloning, clone fallback, copied-WAL consolidation, active WAL writes without source locking, and retries after source changes. See `test_refresh_skips_capture_when_source_inventory_is_unchanged`, `test_same_size_cached_attachment_replacement_triggers_refresh`, `test_source_change_during_refresh_is_reported_with_snapshot_time`, `test_force_refreshes_an_unchanged_source`, and `test_failed_force_refresh_preserves_the_active_pointer` in [test_cache.py](../tests/test_cache.py), plus `test_sync_reuses_unchanged_and_force_rebuilds` in [test_cli.py](../tests/test_cli.py), and `test_real_cow_clone_has_independent_writes`, `test_snapshot_falls_back_to_copy_after_exdev_and_keeps_source_unchanged`, `test_snapshot_consolidates_copied_wal_and_reads_committed_rows`, `test_snapshot_fails_during_active_wal_writes_without_locking_source`, and `test_snapshot_retries_after_a_source_change` in [test_snapshot.py](../tests/test_snapshot.py). These tests use synthetic sources and writers. They do not run a full Goodnotes app sync. The macOS clone test skips on platforms or filesystems without clone support.

This remains a file-copy workflow, not a transaction coordinated with the Goodnotes app. A continuously changing source can make sync fail after three attempts.

## Readers and refreshes

`.sync.lock` serializes full refreshes. A reader acquires `.view.lock` in shared mode before resolving the active pointer and keeps it through its last database or attachment read. Sync copies changed source files without holding `.view.lock`, then takes that lock exclusively to publish the new pointer and prune superseded managed generations. A long reader delays publication and pruning, not the source capture. The Goodnotes app does not take either lock.

The managed cache retains the active generation. Once existing readers release the view lock, a successful refresh removes older managed generations and their annotated-PDF registrations. An explicit `--sandbox` path inside this cache follows the same lock and lifetime rules. The separate `snapshot LIBRARY DESTINATION` command creates a caller-owned copy. Keep its destination outside the managed cache; normal sync does not remove it.

Tests cover unchanged reuse, one active generation after refresh, old-reader retention while capture proceeds, serialized concurrent syncs, and explicit managed-sandbox locking. See `test_refresh_retains_only_active_generated_snapshot_and_removes_registration`, `test_old_reader_holds_snapshot_while_capture_progresses_and_publication_waits`, `test_concurrent_refreshes_serialize_and_second_sees_fresh_cache`, and `test_explicit_managed_sandbox_takes_shared_view_lock` in [test_cache.py](../tests/test_cache.py). `test_render_loads_read_view_only_once` in [test_cli.py](../tests/test_cli.py) checks that one command resolves the read view once.

## Read-only cache access

Sync creates persistent lock files in the CLI cache. A reader opens the existing view lock read-only, acquires a shared lock, and creates no cache files or directories. Read access to the cache is sufficient for browsing, searching, and reading. Export commands also need write access to their output destination; registering an annotated PDF writes separate registration metadata.

Readers reject missing locks, denied access, symlinks, and non-regular lock files. A nonblocking open prevents a raced FIFO from hanging before validation. The opened file's identity is checked again after the shared lock is acquired. Missing locks are never recreated by readers. Stop other CLI processes before using sync to repair a missing lock; deleting a lock while it is held breaks coordination through its original inode. The CLI never removes its lock files during normal operation.

Cache access errors include a stable code and `details` with `operation`, `path`, and `remedy`. `cache_not_initialized` means setup or a required lock is missing, `cache_permission_denied` means the runner lacks access, and `cache_invalid` means the selected cache state failed validation. The CLI does not bypass locking or choose a different cache. Use the same `--data-dir` or `GOODNOTES_DATA_DIR` setting for setup and reading. See [agent setup](usage.md#read-access-for-an-agent).

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

## Registered annotated PDFs

`attach-pdf` checks that a PDF is readable and has the notebook's current page count. The CLI stores registration metadata outside the immutable read view. Before rendering or exporting the `annotated` layer, it checks the page count and PDF content signature again. If the PDF moves, changes, or no longer matches the current notebook page count, the operation fails instead of falling back to background-only output.

These checks cannot verify the PDF's page order or whether annotation export was enabled. Background output reports `includes_handwriting: false`. Annotated output reports `includes_handwriting: null` and `annotation_status: user-provided-unverified`.

See `test_register_rejects_pdf_page_count_mismatch`, `test_registered_pdf_must_match_current_notebook_page_count`, `test_registered_pdf_integrity_detects_same_size_edit_with_restored_mtime`, and `test_annotated_layer_never_falls_back_to_background` in [test_documents.py](../tests/test_documents.py).

## Limits and output conflicts

List and tree results return 100 items by default. `read` accepts at most 20 pages and limits combined text to 12,000 characters by default. `search` returns at most 20 short snippets. `bundle` accepts at most 10 pages and applies one 12,000-character default budget across their text. `read` and `bundle` set `truncated` when the budget cuts text.

Render, export, and bundle output paths must be outside the snapshot and production Goodnotes locations. Existing files are not overwritten. A bundle is prepared in a temporary directory, then its image files and manifest are published into a newly reserved output directory. The manifest is published last. If another process creates the destination first, the CLI fails rather than replacing it.

See `test_read_reports_budget_and_preserves_handwriting`, `test_bundle_budget_and_published_paths`, and `test_bundle_publisher_does_not_replace_racing_directory` in [test_cli.py](../tests/test_cli.py), plus `test_output_file_is_never_overwritten` and output-path checks in [test_documents.py](../tests/test_documents.py) and [test_safety.py](../tests/test_safety.py).

## Structured errors

Expected operation errors return compact JSON on stderr with exit code 1. Invalid arguments return a structured error with exit code 2. These errors do not print a Python traceback. See `test_missing_library_is_structured_stderr` and `test_argument_errors_are_structured` in [test_cli.py](../tests/test_cli.py).
