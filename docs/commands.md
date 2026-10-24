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
| `--fields FIELDS` | All fields | Comma-separated keys to keep. It selects keys in object rows under `items`, `pages`, or `hits` when present; otherwise it selects top-level response keys. An unknown key returns the valid keys for that level. |
| `--expect-snapshot ID` | Unset | Fail with `stale_snapshot` if a cache-backed command opens a different read-view generation. |
| `--version` | N/A | Print the CLI version and exit. |
| `--help` | N/A | Show the CLI or command's help and exit. |

`GOODNOTES_DATA_DIR` takes precedence over the default cache path; `--data-dir` overrides the environment. The same precedence applies to `GOODNOTES_SANDBOX` and `--sandbox`.

Field selection filters object rows while keeping surrounding metadata, including warnings and pagination. Empty row arrays stay empty and still validate requested field names. Responses without object-row arrays, such as `bundle`, use top-level selection. Valid optional response keys are omitted when absent. Cache-backed commands always retain `snapshot`. Declared fields are checked before commands write outputs or cache state. Use `schema COMMAND` for field names; selecting `recognition` requires `pages --with-recognition`.

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

Refreshes the active managed copy and removes superseded managed generations after readers finish. It reads source Library files but does not open source SQLite databases, control Goodnotes, or write to the source library. The result includes `status` (`ready` or `unchanged`), `snapshot` (the generation path), `snapshot_id` (the generation ID), `checked_at`, `source_changed_during_refresh`, and, when available, `captured_at` and `warnings`. A true `source_changed_during_refresh` means the published copy is valid as of `captured_at`; it does not promise that the source stayed unchanged until the command returned.

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

Lists direct children in stored order. The result has an `items` array, `next_offset`, and the active read-view `snapshot` ID. Item rows include `id`, `name`, `kind`, `path`, `parent_id`, `page_count`, and `updated_at`.

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

Lists the notebook's active pages in current order, numbered from 1. The result has a `pages` array, `next_offset`, and the active read-view `snapshot` ID. Page rows include page and notebook IDs, page number, position, available background attachment and PDF-page mapping, dimensions, and annotation count. `--with-recognition` adds per-page availability and index state without returning candidate text. The top-level `annotation_count_source` is `projection.notes_items`; it does not count ink or OCR results.

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
| `--layer LAYER` | `background` | `background` or `annotated` |

Exports all active notebook pages in current order when `--pages` is omitted. For example, `--pages 1-3,7` exports those pages in that listed order. The result includes the output path, `pages` count, `selected_pages`, source, `includes_handwriting`, `annotation_status`, and warnings. The background layer contains PDF page backgrounds and does not overlay native Goodnotes ink. The annotated layer requires a PDF registered with [attach-pdf](#attach-pdf); registration cannot verify whether annotations were included or whether page order matches. Existing output paths are never overwritten; outputs must be outside the snapshot and production Goodnotes locations.

### render

```text
goodnotes render NOTEBOOK --output PNG [--page N] [--layer LAYER] [--dpi N]
```

| Option | Default | Bounds or values |
| --- | --- | --- |
| `--output PNG` | Required | New `.png` path |
| `--page N` | `1` | `1` or greater, within the notebook |
| `--layer LAYER` | `background` | `background` or `annotated` |
| `--dpi N` | `150` | `72` to `300` |

Renders one page for visual inspection. Returns the output path, notebook and page IDs, source page, layer, DPI, annotation status, and warnings. Output-path and annotation limits are the same as [export](#export).

### attach-pdf

```text
goodnotes attach-pdf NOTEBOOK PDF
```

Registers a user-exported full notebook PDF for the `annotated` layer. It checks that the input is a regular, readable PDF with the notebook's current page count, then stores registration metadata in a sidecar tied to this snapshot generation. The command records the PDF path rather than copying it. Keep the file available and unchanged. Page count and file-integrity checks cannot confirm that annotations are present or that the PDF page order matches the notebook. Results report `annotation_status: "user-provided-unverified"` for annotated outputs.

### bundle

```text
goodnotes bundle NOTEBOOK --output DIR [--pages EXPR] [--layer LAYER] [--dpi N] [--max-chars N]
```

| Option | Default | Bounds or values |
| --- | --- | --- |
| `--output DIR` | Required | New directory |
| `--pages EXPR` | `1` | Up to 10 distinct pages, for example `1-3,7` |
| `--layer LAYER` | `background` | `background` or `annotated` |
| `--dpi N` | `150` | `72` to `300` |
| `--max-chars N` | `12000` | `1` to `1000000` characters across all selected pages |

Creates `page-NNNN.png` files and `text.json` in the new directory. The result reports the directory, manifest path, selected pages, layer, truncation status, and active read-view `snapshot` ID. The page images use the same layer and annotation rules as [render](#render). Existing destinations are not overwritten; outputs must be outside the snapshot and production Goodnotes locations.

### doctor

```text
goodnotes doctor
```

Reports active and deleted item counts, tree consistency issues, search-index availability, available content tables, page-metadata gaps, and warnings for the selected read view. It does not refresh the source library.

### schema

```text
goodnotes schema [COMMAND]
```

Prints the command contract without loading notebook data. With no command, it reports the compact CLI overview. With a command name, it returns that command's contract, option metadata, and output-field hints where available. Row-based commands identify the projection level, valid row keys, and a `--fields` example. These keys are derived from the output models; `pages` marks `recognition` optional because it requires `--with-recognition`. Use `goodnotes COMMAND --help` for human-readable help. Treat schema output as a discovery contract that may grow, not as a frozen copy of this full reference.

## Output, errors, and pagination

Successful commands that return data write one JSON value to stdout. Compact JSON is the default; `--text` indents it. Help and version output are plain text. Expected runtime errors write `{"error":{"code":"...","message":"..."}}` to stderr and exit `1`. Errors may include `details` with recovery context; unavailable-index search errors include scoped coverage and PDF fallback guidance. Invalid command syntax or parser-checked option bounds exit `2` with `invalid_arguments`; application-checked values such as an invalid page expression are runtime errors and exit `1`. Keyboard interruption or a broken pipe exits `130`.

Cache-backed commands include the read-view generation ID as `snapshot`. For `ls`, `tree`, `pages`, and `search`, pass `next_offset` as the next `--offset`. For `scan-pdfs`, pass `next_page_offset` as the next `--page-offset`. Include the same generation ID with global `--expect-snapshot ID` on every request in a multi-call sequence. If the command returns `stale_snapshot`, restart the sequence against the new view; do not combine results from different generations. The `snapshot` field from `sync` is a filesystem path; use its `snapshot_id` field for the generation ID. The standalone `snapshot` command's `snapshot` field is also a destination path.
