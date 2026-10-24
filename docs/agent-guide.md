# Agent guide

Run `uv run goodnotes` from the repository root. Start with `uv run goodnotes schema [COMMAND]` for the machine-readable contract or `uv run goodnotes COMMAND --help` for readable options. Put global options such as `--sandbox`, `--fields`, and `--expect-snapshot` before the command. See the [command reference](commands.md) for every option and bound.

## Read an initialized cache

Setup runs `sync` with read access to the source and write access to the cache. Browsing, searching, and reading then need only cache read access, including the existing `.view.lock`. Use the installed `goodnotes` executable directly if your runner cannot update the project environment. Keep the same `--data-dir` or `GOODNOTES_DATA_DIR` selection as setup. `doctor.storage` gives the resolved cache directory, snapshot path, and capture time. Cache permission failures return `cache_permission_denied`; missing setup or lock files return `cache_not_initialized`; malformed cache state returns `cache_invalid`. Follow `error.details` for the path, operation, and remedy. Do not bypass locking, create replacement locks, or silently choose another cache. A missing-lock repair requires stopping other CLI processes before a process with write access runs `sync`.

`doctor --access-only` checks the selected cache or explicit sandbox without opening databases or writing files. It can report access problems even before initialization. Use its optional `--library`, `--scratch-dir`, and `--output-dir` checks to distinguish source read access, temporary scratch access, and export access. Write-access hints are preflight checks, not a successful write test. The CLI does not grant permissions or change runner restrictions.

## Inspect notes while the user studies

For questions about recent edits, refresh once at the start of the agent request. The refresh process needs source read access and cache write access. Use the same cache for the subsequent commands:

```sh
goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
```

Read `snapshot_id` from that result and pass it as `--expect-snapshot SNAPSHOT_ID` to the request's reads, searches, and renders. Do not sync again between those commands unless restarting the request. This avoids repeated metadata scans and prevents mixing generations. An unchanged sync reuses the active generation; changed refreshes reuse verified unchanged files. If sync fails, report the refresh failure rather than silently presenting the old copy as current.

`watch` is optional. Use it when a separate process should refresh continuously, or when the agent can only read the cache. The watcher needs source read access and cache write access; the reading agent does not need source access. Keep it running and use its selected cache. Even with a watcher, pin each request's snapshot; on `stale_snapshot`, restart against the new generation.

When the user asks about notes on a page, use the image as the visual source:

```sh
goodnotes --data-dir /path/to/cli-cache --expect-snapshot SNAPSHOT_ID bundle "Notebook name" --pages 3 --layer native --output /tmp/study-page-3
```

Open the image named in `text.json`. Read `native_annotations`, `annotation_status`, warnings, `snapshot`, and `captured_at` alongside the image. Native reconstruction is approximate. `includes_handwriting: true` only establishes that supported strokes were rendered; coverage and unsupported-object counts limit what an agent can conclude. If coverage is partial, say which reported limitations affect the answer. Missing native data is unknown availability, not proof that the page has no writing. Never substitute a clean background and describe it as the user's complete notes.

Both sync and watch observe files already present on the Mac. They cannot confirm that a particular iPad edit has arrived. A newer `captured_at` alone does not prove that the expected note is present. Compare the actual page image or ask for the expected edit when needed. A refresh error preserves the prior copy, so continued readable output is not evidence of successful refresh.

## Find pages and stored recognition text

Use `info`, `ls`, or `tree` to find a notebook. Use `pages` for its current one-based page numbers. Select notebooks by exact UUID, full path, or unique exact name; use the full path or UUID when a name is ambiguous.

`pages --with-recognition` adds per-page metadata without returning candidate text. `recognition.availability` distinguishes `stored-text`, `no-stored-text`, and `unavailable`; `recognition.index_state` distinguishes `pending`, `not-pending`, and `unknown`. Counts describe stored recognition rows and characters, not ink strokes or transcription accuracy. The page `annotation_count` comes from `projection.notes_items`; it is not an ink-stroke or recognition count.

Read candidate text with `read --source handwriting`. Preserve the source label, raw delimiters, and warnings. Quote only exact raw substrings; label edited wording as an interpretation rather than a verbatim quote or visual transcription. A pending-index warning can appear even when no stored candidate text is returned. `not-pending` does not verify recognition accuracy or freshness. `pdf` text may come from the index or a mapped PDF background in the copy. `textbox` is typed text stored on the page.

## Search and check coverage

Use `search --notebook NOTEBOOK` for one notebook or `search --folder FOLDER` for a subtree. These options are mutually exclusive. If both are omitted, the scope is `/`. Search checks stored text-index rows only. Read `coverage_detail`, `warnings`, and source labels before interpreting an empty result. The reported counts and page-metadata diagnostics describe stored rows in the resolved scope; they do not prove that every page was indexed. Completeness remains `unknown`, including when metadata appears complete. `next_steps` suggests follow-up commands; search does not start a PDF scan automatically.

When a result may be in a copied PDF background, run `scan-pdfs` separately with the same notebook or folder scope. It does not search handwriting, the stored index, or a PDF registered with `attach-pdf`. `next_page_offset` counts scanned pages, not hits. Continue with the same query, scope, and snapshot. `scan_complete` means the scoped page sequence is exhausted. `coverage_complete` is true only when the current scan starts at offset 0, reaches the end, and has no extraction gaps. Check `stop_reason` and warnings for a hit limit, page limit, missing background, missing PDF mapping, unreadable PDF text, or truncated extraction. Even complete PDF-background coverage says nothing about handwriting.

## Keep results comparable

For a paginated sequence, read the `snapshot` ID from its cache-backed result and pass it before every later command as `--expect-snapshot ID`. Follow `next_offset` with `--offset`, or `next_page_offset` with `--page-offset` for `scan-pdfs`. If the CLI returns `stale_snapshot`, restart the sequence against the new view. Do not combine results from different snapshot IDs. The `snapshot` value from `sync` or the standalone `snapshot` command is a path; use `snapshot_id` from `sync` or a generation ID from a cache-backed read result.

## Project fields and interpret output

Use `uv run goodnotes schema COMMAND` to discover row projections and valid fields. Row-based command schemas include an output projection and a `--fields` example. For example, `uv run goodnotes --fields number,recognition pages "Notebook name" --with-recognition` keeps page numbers and recognition metadata. Add `--with-recognition` whenever selecting `recognition`. For search hits, `--fields path,page,source,text` keeps the source label with the text. Field projection retains surrounding response metadata, including warnings, pagination, and `snapshot`; use keys valid for the selected row level.

Successful data responses are compact JSON by default. `--text` only indents it. Expected runtime errors are JSON on stderr with exit code `1`; invalid command syntax and parser-checked bounds exit `2`. Page-expression and other application-checked errors are runtime errors. An interrupt or broken pipe exits `130`. Read stderr instead of treating empty stdout as a result.

## Use exports carefully

`export --pages EXPR` selects distinct one-based pages in the listed order; omitting it exports all pages. It has no 20-page cap. `render`, `export`, and `bundle` require new destinations outside the snapshot and production Goodnotes locations. The default `background` layer does not overlay native ink. Use explicit `--layer native` for supported native pen reconstruction and retain its coverage diagnostics. An `annotated` layer uses a user-supplied PDF registered with `attach-pdf`; the CLI checks the file and page count but cannot verify annotation inclusion or page order. Preserve `annotation_status: "user-provided-unverified"` and warnings with any conclusion.

For source and annotation details, see [PDFs and handwriting](pdf-and-handwriting.md). For development and checks, see [Contributing](../CONTRIBUTING.md).
