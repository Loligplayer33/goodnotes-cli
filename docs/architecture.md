# Architecture

The normal workflow is `goodnotes sync`, followed by commands such as `goodnotes ls` or `goodnotes read`. On macOS, `sync` discovers the local Goodnotes Library and refreshes the CLI cache. Browse commands use a validated local read view. The CLI reads Goodnotes files but never opens a source SQLite database, controls the app, or writes to the source library.

```text
Goodnotes Library on macOS
        |
        | sync: stat inventory, then capture changed files and validate copy
        v
Managed CLI cache: active generation + active.json
        |
        +-- safety: validate the active copy and reject live library roots
        +-- library: read folders, notebooks, pages, and stored text
        +-- documents: read PDF pages, export, render, and attach PDFs
        +-- CLI: emit compact JSON by default, or indented JSON with --text
```

The default cache is `~/.cache/goodnotes-cli`, based on `XDG_CACHE_HOME`. `GOODNOTES_DATA_DIR` or the global `--data-dir` option selects another cache. The `--sandbox` option is an explicit development override for an existing marked copy. `snapshot LIBRARY DESTINATION` creates a separate named copy for a caller that needs a persistent view. Keep named snapshots outside the managed cache; `sync` prunes only its managed generations.

## Refresh and read lifecycle

`sync` holds the cache's persistent `.sync.lock` for the full refresh, so concurrent refreshes run in sequence. It inventories the required databases, SQLite sidecars, optional native `notes_main.db` tree, and attachments. It also compares the saved stat inventory for the active cache tree. When the source path and both inventories match, `sync` reuses the active generation without hashing file contents. This catches ordinary cache damage without reading every byte. `sync --force` bypasses reuse and verifies a new capture. Read commands do not refresh automatically. The explicit foreground `watch` command polls metadata and calls the same refresh function after changes settle.

When the inventory changes, `sync` builds a staging generation. Unchanged files reuse the previous verified copy only when source and cache metadata match. Reuse trusts the previous digest; it does not rehash unchanged bytes. Changed files receive a fresh content check. On macOS the copier tries independent APFS clones; elsewhere it falls back to independent byte copies. It never hardlinks snapshot files. SQLite reuse requires the entire original database-and-sidecar family to match, since WAL consolidation changes the completed database bytes. Native capture includes CURRENT, manifests, SSTs, WALs, and other regular store files without opening the source engine. Source and completed-cache fingerprints remain separate. After copied-WAL consolidation and projection validation, the marker is written last. Detected source changes trigger up to three attempts; failure preserves the previous view.

With `--verbose`, `capture_stats` reports captured and reused files, source bytes hashed, clone and byte-copy counts, and capture time. Byte counts are logical file sizes, not unique physical disk allocation. Activity counters describe the successful attempt; capture time includes failed attempts. An unchanged refresh reports zero capture activity.

After capture, `sync` takes a final source inventory before it waits for the exclusive `.view.lock` to publish. It compares that inventory with the one taken before capture, including any earlier capture attempts. If they differ, sync publishes the validated view, sets `source_changed_during_refresh: true`, and warns that the view is valid as of `captured_at`. If they match, the flag is false. That means the two checks matched, not that the source stayed unchanged through command return. Updates after the final check, including while sync waits to publish, are detected by the next sync. A changed inventory leaves out the reusable source fingerprint, so the next sync captures a fresh view. `captured_at` is the validated view's capture time; sync does not promise the latest source state at return.

Readers acquire the persistent `.view.lock` in shared mode before resolving `active.json` and keep it through their final database or attachment read. `sync` does not hold this lock during inventory or capture. It takes the lock exclusively only to update or publish the active pointer and prune old managed generations. A long-running reader can delay publication and cleanup while capture continues. Once readers release the lock, the cache keeps the active generation and removes superseded managed generations. Explicit `--sandbox` paths that point into this managed cache follow the same lock and retention rules. Custom named snapshots outside the cache are not pruned.

Sync creates persistent locks. Readers open the existing view lock read-only and create no cache paths. The lock must be regular, is opened without following symlinks, and retains the same identity through acquisition. Readers block publication and pruning until they finish. Missing or inaccessible locks fail with actionable cache errors. Do not delete or replace locks during use. Restricted runners need read access to the selected private cache. Native rendering also needs writable temporary scratch outside that cache; it clones the copied native store there and removes it after extraction.

If a refresh is interrupted with Ctrl-C, the CLI removes its partial staging directory. If the process is killed before cleanup, the next `sync` removes abandoned staging directories. A crash never makes an incomplete staging directory active. The Goodnotes app does not participate in these locks. The copy is not a transaction coordinated with the app, so a source that keeps changing can make `sync` fail after three attempts.

## Package boundaries

`models` contains the data structures shared by the other modules. `errors` defines expected failures that the CLI can show without a Python traceback.

`safety` resolves the active read view or explicit development sandbox, checks its marker, and prevents normal commands from opening a live Goodnotes library. Keep path checks here so every command follows the same rule.

`snapshot` copies databases and attachments to a new cache snapshot. It includes SQLite sidecars such as WAL files when they exist, verifies changed files and source stability, consolidates copied database sidecars, checks the copied projection database, and writes a `goodnotes-snapshot.json` marker last. `sync` switches the cache's active pointer only after snapshot creation returns. Reader connections open completed copies in read-only mode.

Consolidation may write only to independent, unpublished staging copies after capture verification. For WAL families without a rollback journal, it makes only staging files writable, discards the copied SHM index and lets SQLite rebuild it, checkpoints committed WAL pages into the copied main file, and switches the copy to DELETE journal mode. It requires a completed checkpoint and removes only empty sidecars after closing the connection. This preserves copy-on-write sharing for unchanged main-file pages. Non-WAL databases and families with a rollback journal retain the read-only backup procedure into a separate staging database. Engines never open source files. Completed snapshots remain immutable.

`library` maps stored rows to folders, notebooks, ordered pages, and text search results. It labels each text result by source. `read` uses indexed PDF text when available, then extracts text from the mapped PDF background in the copied library if the index has no text. `search` uses indexed rows. PDF text may omit annotations and cannot read image-only writing. Handwriting results are stored recognition candidates. `textbox` results are typed text stored on the page. These sources retain their provenance; recognition candidates are not a visual rendering of handwriting.

The library's search database uses a Goodnotes-specific full-text-search tokenizer. The CLI reads the ordinary stored rows with Python's SQLite support instead of querying virtual FTS tables that require that tokenizer.

`text_index` contains read-only, batched queries for stored recognition counts, verified PDF bindings, raw indexed text rows, and scoped diagnostics. `library` owns the connections, selectors, page numbers, and mapping of raw rows into domain search results. It derives PDF page identities from the projection's document, attachment, and PDF page. Read, search, and coverage use the same binding checks so stale index pointers cannot assign another page's text. Metadata counts do not establish source completeness. Recognition status reports stored text availability and a pending-index flag, not whether a page has ink or whether recognition is accurate.

`documents` maps notebook pages to backgrounds, renders PNGs, and exports PDFs. Explicit selections preserve listed order. Standalone render/export default to `background`, which omits native ink. The study `bundle` command defaults to `native`, composing supported copied pen/highlighter geometry over its mapped background with coverage counts, capture time, and approximation warnings. `native` opens RocksDB only on disposable independent scratch; fresh options keep engine paths there and never load copied OPTIONS configuration. `annotations` decodes the observed protobuf, Apple LZ4, and TPL formats with bounded expansion. Unsupported geometry, ambiguous versions, unknown visibility, and transforms remain explicit. Full fidelity and all annotation types are unverified. Missing native data never silently falls back to background output.

`attach-pdf` retains the separate `annotated` layer for a registered user export. It checks file integrity and page count, but not page order or annotation inclusion. Registration stores a private sidecar path, without copying the PDF. Lookup creates no directories. Annotated results preserve `includes_handwriting: null` and `annotation_status: user-provided-unverified`.

`cli` defines the Typer commands and translates expected errors into compact JSON by default. Routine responses use concise profiles that retain interpretation limits while removing incidental metadata and repeated diagnostics. Global `--verbose` includes full metadata; `--fields` explicitly projects the full field contract. `--text` indents JSON for inspection and `--json` selects compact formatting. `bundle` returns page evidence directly; its full on-disk manifest is independent of the response profile. `schema` describes both output shapes without reading notebook data. Keep command parsing and presentation separate from storage queries and PDF handling.

## Data and numbering

Public page numbers start at 1, including `--page` and both ends of a `--pages` range. Internal ordering may use zero-based PDF page indexes, so conversion belongs at the CLI or document boundary.

Listing and tree commands return at most 100 items by default and accept `--limit` and `--offset`. Stored-index `search` returns up to 20 snippets by default, identifies its coverage as `stored-index`, and includes warnings for missing text tables or page metadata. `scan-pdfs` searches copied PDF backgrounds independently of that index. It scans 200 pages by default and accepts a `--page-limit` up to 250. Its `next_page_offset` cursor counts pages scanned, not hits. It reports missing, unmapped, unreadable, or truncated backgrounds, and it does not search handwriting.

Paged browse results include the read-view generation ID as `snapshot`. Clients can send that value back through the global `--expect-snapshot ID` option. If another `sync` has changed the active generation, the command returns a structured `stale_snapshot` error. A client should restart its page sequence against the new generation instead of mixing result pages from two views.

`read` defaults to page 1 and accepts no more than 20 requested pages in one call. Its `--max-chars` option caps returned text. `bundle` accepts up to 10 pages and applies one `--max-chars` budget across the text in its manifest. Both outputs report truncation.

The `read` and `search` commands preserve source labels. An agent can distinguish PDF text, handwriting recognition candidates, and typed text without guessing which mechanism produced a string. PDF text may omit annotations. Recognition candidates do not reproduce their visual position, shape, or color. The `bundle` command writes page images and source-labeled text for up to 10 requested pages. It defaults to PDF text, and its `--source` override filters text without changing the rendered image. The selected source is always recorded as `text_source` in the manifest and response.

## Dependencies and execution

Python 3.11 or newer is required. Typer handles commands, PyMuPDF reads and renders PDFs, and the standard library handles SQLite. Native rendering additionally requires a compatible RocksDB shared library and writable scratch. Install a current engine with `brew install rocksdb` on macOS, or select an absolute shared-library path through `GOODNOTES_ROCKSDB_LIBRARY`. `GOODNOTES_SCRATCH_DIR` selects scratch storage. Large native stores require copy-on-write support; fallback byte copying is capped at 64 MiB per extraction. `doctor --access-only` checks filesystem access without creating files or opening databases and reports optional runtime availability separately. `uv` manages the Python project environment.

Run the CLI from the repository with `uv run goodnotes ...`. Do not install project dependencies globally or add a runtime dependency without a concrete need. Development tools live in the `dev` dependency group. Global options such as `--data-dir`, `--sandbox`, `--fields`, `--text`, `--json`, and `--expect-snapshot` go before the command name.

## Design references

Commands use familiar names, explicit flags, and help text that can be read without opening the source. Those choices follow the [Command Line Interface Guidelines](https://clig.dev/) and the help conventions in the [GitHub CLI reference](https://cli.github.com/manual/gh_help_reference).

The `src/` layout keeps the installable package separate from project files, following the [uv project layout guidance](https://docs.astral.sh/uv/concepts/projects/layout/). [Typer](https://typer.tiangolo.com/) provides the typed command tree and option parsing.

## Safety rules

- In normal use, `sync` checks source file metadata and copies changed files into the CLI cache. It does not write to Goodnotes or open the source databases.
- Reader SQLite connections open completed copies in read-only mode. Consolidation can write only to independent, unpublished staging copies.
- `.sync.lock` serializes refreshes. `.view.lock` protects pointer resolution, active readers, publication, and pruning. Neither lock is placed in the Goodnotes library.
- Source copying is not coordinated with the Goodnotes app. Detected changes trigger bounded retries; sustained changes fail the refresh and preserve the previous active view.
- Managed cache generations can expire at the next successful refresh after readers finish. A caller-created snapshot outside the managed cache remains until its owner removes it.
- `--sandbox` is a development override for an existing marked copy.
- Keep snapshots, annotated-PDF registrations, and extracted note content outside the repository.
- Use generated fixtures for automated tests and examples.
- Report unsupported annotation rendering as a capability limit. Do not label OCR candidates as a visual reconstruction.

See [Robustness and failure behavior](robustness.md) for concurrency cases, recovery behavior, coverage warnings, and the tests that exercise these boundaries.
