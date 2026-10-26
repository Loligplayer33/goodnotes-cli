# Command reference

Run the CLI from this repository with `uv run goodnotes`. Global options go before the command. For example:

```sh
uv run goodnotes --text ls --limit 50
uv run goodnotes --expect-snapshot SNAPSHOT_ID pages "Course Notes"
```

Use `uv run goodnotes --help` for the command index, `uv run goodnotes COMMAND --help` for readable examples and options, and `uv run goodnotes schema COMMAND` for structured arguments, options, defaults, bounds, and output-field hints. Plain `schema` returns the compact overview.

## Command index

| Command | Use |
| --- | --- |
| [sync](#sync) | Refresh the managed local read view. |
| [watch](#watch) | Refresh the copy automatically while a foreground process runs. |
| [snapshot](#snapshot) | Create a separate named copy of a Library directory. |
| [ls](#ls) | List a folder's direct children. |
| [tree](#tree) | List descendants within a depth and result limit. |
| [info](#info) | Resolve an item selector. |
| [pages](#pages) | List a notebook's pages in current order. |
| [read](#read) | Read source-labelled text from notebook pages. |
| [search](#search) | Search text stored in the search index. |
| [scan-pdfs](#scan-pdfs) | Search copied PDF backgrounds. |
| [export](#export) | Export a notebook to PDF. |
| [render](#render) | Render one notebook page to PNG. |
| [attach-pdf](#attach-pdf) | Register a user-exported PDF for the annotated layer. |
| [bundle](#bundle) | Write selected page images and text to a new directory. |
| [doctor](#doctor) | Inspect the active read view and its available text sources. |
| [schema](#schema) | Print the CLI's machine-readable command contract. |

## Global options

These options are accepted before a command. They apply only where the command uses them.

| Option | Default or environment | Effect |
| --- | --- | --- |
| `--sandbox PATH` | `GOODNOTES_SANDBOX`; unset by default | Read from an existing marked snapshot instead of the active managed view. |
| `--data-dir PATH` | `GOODNOTES_DATA_DIR`; otherwise `$XDG_CACHE_HOME/goodnotes-cli`, or `~/.cache/goodnotes-cli` | Select the managed cache location. |
| `--text` | Off | Indent JSON for inspection. |
| `--json` | On | Emit compact JSON. If both `--text` and `--json` are given, compact JSON wins. |
| `--verbose` | Off | Include full JSON metadata instead of the concise response profile. |
| `--fields FIELDS` | All fields | Comma-separated keys to keep. It selects keys in object rows under `items`, `pages`, or `hits` when present; otherwise it selects top-level response keys. An unknown key returns the valid keys for that level. |
| `--expect-snapshot ID` | Unset | Fail with `stale_snapshot` if a cache-backed command opens a different read-view generation. |
| `--version` | N/A | Print the CLI version and exit. |
| `--help` | N/A | Show the CLI or command's help and exit. |

`GOODNOTES_DATA_DIR` takes precedence over the default cache path; `--data-dir` overrides the environment. The same precedence applies to `GOODNOTES_SANDBOX` and `--sandbox`.

Field selection filters object rows while keeping surrounding metadata, including warnings and pagination. Empty row arrays stay empty and still validate requested field names. Responses without object-row arrays, such as `bundle`, use top-level selection. Valid optional response keys are omitted when absent. Cache-backed commands always retain `snapshot`. Declared fields are checked before commands write outputs or cache state. Use `schema COMMAND` for field names; selecting `recognition` requires `pages --with-recognition`.

Concise profiles apply by default to sync/watch, listings, item and page metadata, page reads, renders, exports, and bundles. Other commands retain their full responses. Sync/watch omit the snapshot path and detailed capture counters. Listings omit parent IDs and timestamps. Page reads share the document ID at the response level. Native summaries keep coverage, fidelity status, object counts, unsupported reasons, and warnings. `--verbose` restores full metadata; `--fields` deliberately projects the full field contract instead of the concise profile. Errors are always complete. Use `schema COMMAND` for both output shapes.

## Names, folders, and pages

`NOTEBOOK` accepts an exact UUID, exact full library path, or unique exact name. `FOLDER` accepts the same selector forms but must resolve to a folder. An ambiguous name fails instead of choosing an item. Use the full path or UUID when names repeat. `ls` and `tree` use `/` when `--folder` is omitted. `search` and `scan-pdfs` use `/` when neither scope option is supplied.

Public page numbers start at 1. Page expressions such as `1-3,7` preserve the listed order and reject duplicates, empty entries, reversed ranges, and pages outside the notebook. Page-count limits depend on the command: `read` accepts at most 20, `bundle` at most 10, and `export` has no 20-page cap.

## Commands

### sync

```text
goodnotes sync [--library PATH] [--force | --no-force]
```

| Option | Default | Effect |
| --- | --- | --- |
| `--library PATH` | Goodnotes Library at `~/Library/Containers/com.goodnotesapp.x/Data/Library` on macOS | Choose the source Library directory. |
| `--force` / `--no-force` | `--no-force` | Capture and validate a fresh generation even when inventory checks match. |

Refreshes the active managed copy and removes superseded managed generations after readers finish. It reads source Library files but does not open source SQLite databases, control Goodnotes, or write to the source library. The result includes `status` (`ready` or `unchanged`), `snapshot_id`, `checked_at`, `source_changed_during_refresh`, and, when available, `captured_at` and `warnings`. Global `--verbose` adds the `snapshot` generation path and detailed capture counters. A true `source_changed_during_refresh` means the published copy is valid as of `captured_at`; it does not promise that the source stayed unchanged until the command returned.

With global `--verbose`, `capture_stats` reports logical file and byte counts for captured, reused, cloned, copied, and hashed data. Unchanged files can reuse a prior verified digest when both source and cached-file metadata match. A clone shares storage blocks but has an independent file identity; byte counts do not measure physical disk allocation. `--force` bypasses reuse. Sync includes the optional `notes_main.db` native store and its WAL files. Do not combine `sync` with the global `--sandbox` option; choose the source with `--library`.

### watch

```text
goodnotes watch [--library PATH] [--interval SECONDS] [--debounce SECONDS] [--iterations N]
```

| Option | Default | Bounds or effect |
| --- | --- | --- |
| `--library PATH` | Same discovery as `sync` | Source Library directory; excludes global `--sandbox`. |
| `--interval SECONDS` | `2` | `0.05` to `3600` seconds between metadata checks |
| `--debounce SECONDS` | `1` | `0` to `60` seconds to coalesce edits before capture |
| `--iterations N` | Unset | Stop after `N` polling iterations, at least `1`; otherwise run until interrupted. |

Runs serial copied-data refreshes. Compact output is one JSON object per event: `ready`, `updated`, `recovered`, or `error`. Unchanged checks after startup emit nothing. Success events contain the sync result plus `event` and `iteration`; error events contain structured `error` details and `retry_in_seconds`. Transient failures leave the prior copy selected and retry with backoff. If a bounded run ends with an unrecovered error, it exits `1`; Ctrl-C exits `130`. With `--text`, each event is indented across multiple lines.

Watch checks files on the Mac. It does not confirm that the latest iPad edit has reached Goodnotes on this Mac, and it does not start a background service after the command exits.

### snapshot

```text
goodnotes snapshot LIBRARY DESTINATION
```

Creates an independent, validated copy at a new destination. The destination must not already exist and must not overlap the source Library. The result is `{"snapshot":"DESTINATION","status":"ready"}`; here `snapshot` is the destination path, not the generation ID accepted by `--expect-snapshot`. This command does not make the copy the active managed view.

### ls

```text
goodnotes ls [--folder FOLDER] [--limit N] [--offset N]
```

| Option | Default | Bounds |
| --- | --- | --- |
| `--folder FOLDER` | `/` | Folder selector; see [Names, folders, and pages](#names-folders-and-pages). |
| `--limit N` | `100` | `1` to `1000` items |
| `--offset N` | `0` | `0` or greater |

Lists direct children in stored order. The result has an `items` array, `next_offset`, and the active read-view `snapshot` ID. Item rows include `id`, `name`, `kind`, `path`, and `page_count`; global `--verbose` adds `parent_id` and `updated_at`.

### tree

```text
goodnotes tree [--folder FOLDER] [--depth N] [--limit N] [--offset N]
```

| Option | Default | Bounds |
| --- | --- | --- |
| `--folder FOLDER` | `/` | Folder selector; see [Names, folders, and pages](#names-folders-and-pages). |
| `--depth N` | `3` | `1` to `50` levels |
| `--limit N` | `100` | `1` to `1000` items |
| `--offset N` | `0` | `0` or greater |

Lists descendants in stored sibling order. The result has `items`, `next_offset`, and the active read-view `snapshot` ID. Use `next_offset` as the next `--offset`.

### info

```text
goodnotes info NOTEBOOK
```

Resolves an active folder or notebook by UUID, full path, or unique name. Returns the item record. Ambiguous or unreachable selectors fail; the CLI does not guess.

### pages

```text
goodnotes pages NOTEBOOK [--limit N] [--offset N] [--with-recognition | --no-with-recognition]
```

| Option | Default | Bounds |
| --- | --- | --- |
| `--limit N` | `100` | `1` to `1000` pages |
| `--offset N` | `0` | `0` or greater |
| `--with-recognition` / `--no-with-recognition` | `--no-with-recognition` | Add or omit stored recognition status metadata. |

Lists the notebook's active pages in current order, numbered from 1. The result has a `pages` array, shared `document_id`, `next_offset`, and the active read-view `snapshot` ID. Page rows include their IDs, numbers, and annotation counts. Global `--verbose` adds position, background/PDF mapping, dimensions, and per-row document IDs. `--with-recognition` adds per-page availability and index state without returning candidate text. The top-level `annotation_count_source` is `projection.notes_items`; it does not count ink or OCR results.

### read

```text
goodnotes read NOTEBOOK [--page N | --pages EXPR] [--source SOURCE] [--max-chars N]
```

| Option | Default | Bounds or values |
| --- | --- | --- |
| `--page N` | Page `1` if neither page option is given | `1` or greater |
| `--pages EXPR` | Unset | Up to 20 distinct pages, for example `1-3,7` |
| `--source SOURCE` | `all` | `all`, `pdf`, `handwriting`, or `textbox` |
| `--max-chars N` | `12000` | `1` to `1000000` characters across the selected pages |

Use either `--page` or `--pages`, not both. Returns a `pages` array of page IDs and source-labelled `parts`, plus `truncated` and the active read-view `snapshot` ID. PDF text may come from the stored index or from a mapped PDF background in the copy. Handwriting text is stored recognition output, not a visual reading of ink. It preserves raw candidate delimiters. Label any cleaned reconstruction as an interpretation, not an exact quotation. A pending-index warning may appear even when no candidate text is returned. See [PDF and handwriting](pdf-and-handwriting.md) for source limits.

### search

```text
goodnotes search QUERY [--folder FOLDER | --notebook NOTEBOOK] [--source SOURCE] [--limit N] [--offset N]
```

| Option | Default | Bounds or values |
| --- | --- | --- |
| `--folder FOLDER` | Unset | Folder selector; mutually exclusive with `--notebook`. |
| `--notebook NOTEBOOK` | Unset | One notebook by UUID, path, or unique exact name; mutually exclusive with `--folder`. |
| `--source SOURCE` | `all` | `all`, `pdf`, `handwriting`, or `textbox` |
| `--limit N` | `20` | `1` to `100` hits |
| `--offset N` | `0` | `0` or greater |

Finds literal, case-insensitive substrings in stored search-index rows. The result includes `hits`, `next_offset`, `coverage: "stored-index"`, `coverage_detail`, `warnings`, and the active read-view `snapshot` ID. `coverage_detail` reports the resolved scope, source-specific stored-text counts, metadata diagnostics, warnings, and next steps. Its completeness remains `unknown`; metadata rows do not prove index coverage. Search does not start a PDF scan automatically. Run [scan-pdfs](#scan-pdfs) separately when copied PDF text may be missing from the index.

### scan-pdfs

```text
goodnotes scan-pdfs QUERY [--folder FOLDER | --notebook NOTEBOOK] [--limit N] [--page-limit N] [--page-offset N]
```

| Option | Default | Bounds |
| --- | --- | --- |
| `--folder FOLDER` | Unset | Folder selector; mutually exclusive with `--notebook`. |
| `--notebook NOTEBOOK` | Unset | One notebook by UUID, path, or unique exact name; mutually exclusive with `--folder`. |
| `--limit N` | `20` | `1` to `100` hits |
| `--page-limit N` | `200` | `1` to `250` notebook pages scanned per call |
| `--page-offset N` | `0` | `0` or greater |

Searches extracted text from copied PDF backgrounds only. It does not search the stored index, handwriting, or a PDF registered with `attach-pdf`. The result includes `hits`, resolved `scope`, `total_pages` for that scope, `pages_scanned`, `next_page_offset`, `scan_complete`, `coverage_complete`, `stop_reason`, `warnings`, and the active read-view `snapshot` ID. `stop_reason` is `complete`, `hit-limit`, or `page-limit`. `scan_complete` means no pages remain in the scoped sequence. `coverage_complete` is true only for a scan starting at offset `0` that reaches the end without extraction gaps; it does not describe handwriting or registered PDFs. Continue with `--page-offset` set to `next_page_offset` and keep the query, scope, and snapshot fixed. The cursor counts scanned pages, not matching hits. Warnings may report missing, unmapped, unreadable, or truncated background pages.

### export

```text
goodnotes export NOTEBOOK --output FILE [--pages EXPR] [--layer LAYER]
```

| Option | Default | Values |
| --- | --- | --- |
| `--output FILE` | Required | New `.pdf` path |
| `--pages EXPR` | All pages | Distinct one-based page numbers; preserves listed order, with no 20-page cap |
| `--layer LAYER` | `background` | `background`, `native`, or `annotated` |

Exports all active notebook pages in current order when `--pages` is omitted. For example, `--pages 1-3,7` exports those pages in that listed order. The result includes the output path, `pages` count, `selected_pages`, source, `includes_handwriting`, `annotation_status`, and warnings. Native output also reports stroke, filled-path, and image counts; images do not count as handwriting. The background layer does not overlay native Goodnotes ink. The native layer reconstructs supported objects from the copied native store and reports coverage; it never silently substitutes background-only output when native data is unavailable. See [native ink](pdf-and-handwriting.md#native-ink-without-manual-pdf-exports) for setup and limitations. The annotated layer requires a PDF registered with [attach-pdf](#attach-pdf); registration cannot verify annotation inclusion or page order. Existing output paths are never overwritten; outputs must be outside the snapshot and production Goodnotes locations.

### render

```text
goodnotes render NOTEBOOK [--output PNG] [--page N] [--layer LAYER] [--dpi N]
```

| Option | Default | Bounds or values |
| --- | --- | --- |
| `--output PNG` | Managed temporary cache | New persistent `.png` path when supplied |
| `--page N` | `1` | `1` or greater, within the notebook |
| `--layer LAYER` | `background` | `background`, `native`, or `annotated` |
| `--dpi N` | `150` | `72` to `300` |

Renders one page for visual inspection. Returns the output path, notebook and page IDs, source page, layer, DPI, annotation status, and warnings. Native output also includes a `native_annotations` summary with separate `rendered_strokes`, `rendered_paths`, and `rendered_images` counts. Native images do not count as handwriting. Without `--output`, the response includes `temporary: true` and follows the [artifact retention policy](#artifact-retention). Explicit output-path and annotation limits are the same as [export](#export).

### attach-pdf

```text
goodnotes attach-pdf NOTEBOOK PDF
```

Registers a user-exported full notebook PDF for the `annotated` layer. It checks that the input is a regular, readable PDF with the notebook's current page count, then stores registration metadata in a sidecar tied to this snapshot generation. The command records the PDF path rather than copying it. Keep the file available and unchanged. Page count and file-integrity checks cannot confirm that annotations are present or that the PDF page order matches the notebook. Results report `annotation_status: "user-provided-unverified"` for annotated outputs.

### bundle

```text
goodnotes bundle NOTEBOOK [--output DIR] [--pages EXPR] [--source SOURCE] [--layer LAYER] [--dpi N] [--max-chars N]
```

| Option | Default | Bounds or values |
| --- | --- | --- |
| `--output DIR` | Managed temporary cache | New persistent directory when supplied |
| `--pages EXPR` | `1` | Up to 10 distinct pages, for example `1-3,7` |
| `--source SOURCE` | `pdf` | Text sources: `all`, `pdf`, `handwriting`, or `textbox`. Does not change images. |
| `--layer LAYER` | `native` | `background`, `native`, or `annotated` |
| `--dpi N` | `150` | `72` to `300` |
| `--max-chars N` | `12000` | `1` to `1000000` characters across all selected pages |

Creates `page-NNNN.png` files and `text.json` in a managed temporary directory, or the new persistent directory supplied through `--output`. By default, it returns concise `page_evidence` with native annotated images and PDF text, plus the manifest path, selected pages, layer, text source, truncation status, capture time, and active read-view `snapshot` ID. Native evidence includes separate stroke, filled-path, and image counts; images do not count as handwriting. Managed responses include `temporary: true`. Native cache misses require the optional runtime and copied store; a previously verified cached image can be reused without opening the engine. If a page-local native decode, mapping, or rendering error occurs in a multi-page request and at least one selected page renders, the bundle returns every selected row in order with `status: "partial"`, `failed_pages`, and a warning. Failed rows retain source-labelled text and a structured `render_error`, set `image` to null, and omit successful-render metadata. Failed pages never become background-only images. A single-page or all-pages failure remains a structured error and publishes no bundle. Engine, safety, source-integrity, output, and other global failures remain fatal. Explicit destinations are not overwritten; outputs must be outside the snapshot and production Goodnotes locations.

### Artifact retention

Managed renders and bundles share a separate CLI-owned `artifacts/` directory under the selected data directory. The cap is 100,000,000 bytes of unique retained PNG payloads, manifests, and entry metadata, excluding small fixed control files and in-progress staging. PNGs are content-addressed and hard-linked only between generated artifacts, so identical images count once. Goodnotes files and snapshot files are never linked into this cache.

Matching renders from the same pinned view reuse their PNGs. Across new snapshots, the CLI revalidates and renders the page, then shares storage if the PNG bytes are identical. Bundle text is queried for every request, and current text and provenance determine its manifest. Changes to rendering settings, source inputs, or the renderer version invalidate image reuse.

Entries expire after 86,400 seconds since their last CLI use. Expired and least-recently-used entries are removed on subsequent managed artifact operations. Entries used within the last five minutes are protected so another tool can open returned image paths. If a result cannot fit without removing protected artifacts, the command returns `artifact_cache_capacity`; retry later or use `--output`. Retention does not delete explicitly saved outputs, named snapshots, source files, or unrelated directories. No background cleaner runs after the CLI exits.

Metadata and text commands still require only read access to the library cache. Default visual requests also need write access to artifact storage. A reader with read-only cache access can use a writable explicit `--output` destination instead.

Evidence appears directly in stdout, so an agent can read the text and open absolute image paths without another manifest-read call. Concise output shares the document ID and keeps one page ID and image path per page, source-labeled text, annotation coverage, and distinct warnings. Global `--verbose` includes full nested metadata. The on-disk `text.json` retains full diagnostics regardless of output mode. The same character budget and truncation flags apply to stdout and the manifest.

`--source` filters text using the same rules as `read` and is always recorded as `text_source` in stdout and the manifest. Use `--source all` to include stored recognition candidates and typed text. Use `--layer background` when deliberately requesting clean page images, or `--layer annotated` for a registered PDF export. These overrides preserve the original browsing, reading, and export capabilities.

### doctor

```text
goodnotes doctor [--access-only] [--library PATH] [--scratch-dir PATH] [--output-dir PATH]
```

Reports active and deleted item counts, tree consistency issues, search-index availability, available content tables, page-metadata gaps, and warnings for the selected read view. It does not refresh the source library.

`storage` contains the resolved managed `cache_dir`, selected `snapshot_path`, and `captured_at` from the snapshot marker. `cache_dir` is `null` for a caller-owned named snapshot; `captured_at` is `null` when an older marker has no capture time. The result also retains the generation ID as `snapshot`.

`--access-only` checks filesystem access without opening a database or writing files. It works before cache initialization and honors global `--data-dir` or `--sandbox`. The other three options require `--access-only`: `--library` checks an explicitly selected source, `--scratch-dir` selects the native scratch directory, and `--output-dir` checks an output directory. The result separates checks with `ok`, `missing`, `denied`, or `invalid` status and includes remedies. Write-access hints do not prove that an actual write will succeed. Doctor does not change permissions, create locks, or repair a cache.

The access report also includes `artifact_dir` and an optional `artifact_directory` check. For an uncreated cache directory it reports the nearest existing parent that needs write/search access. Artifact storage is optional for metadata readers and commands with an explicit output, so this check does not make an otherwise usable read view unavailable.

### schema

```text
goodnotes schema [COMMAND]
```

Prints the command contract without loading notebook data. With no command, it reports the compact CLI overview. With a command name, it returns that command's contract, option metadata, and output-field hints where available. Row-based commands identify the projection level, valid row keys, and a `--fields` example. These keys are derived from the output models; `pages` marks `recognition` optional because it requires `--with-recognition`. Use `goodnotes COMMAND --help` for human-readable help. Treat schema output as a discovery contract that may grow, not as a frozen copy of this full reference.

## Output, errors, and pagination

Successful commands that return data write one JSON value to stdout. `watch` emits a sequence of event objects. Compact JSON is the default; `--text` indents it. Help and version output are plain text. Expected runtime errors write `{"error":{"code":"...","message":"..."}}` to stderr and exit `1`; watcher retry errors are events on stdout. Errors may include `details` with recovery context; unavailable-index search errors include scoped coverage and PDF fallback guidance. Invalid command syntax or parser-checked option bounds exit `2` with `invalid_arguments`; application-checked values such as an invalid page expression are runtime errors and exit `1`. Keyboard interruption or a broken pipe exits `130`.

Cache-backed commands include the read-view generation ID as `snapshot`. For `ls`, `tree`, `pages`, and `search`, pass `next_offset` as the next `--offset`. For `scan-pdfs`, pass `next_page_offset` as the next `--page-offset`. Include the same generation ID with global `--expect-snapshot ID` on every request in a multi-call sequence. If the command returns `stale_snapshot`, restart the sequence against the new view; do not combine results from different generations. The `snapshot` field from `sync` is a filesystem path; use its `snapshot_id` field for the generation ID. The standalone `snapshot` command's `snapshot` field is also a destination path.
