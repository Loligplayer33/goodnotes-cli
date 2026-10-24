# Agent guide

Run `uv run goodnotes` from the repository root. Start with `uv run goodnotes schema [COMMAND]` for the machine-readable contract or `uv run goodnotes COMMAND --help` for readable options. Put global options such as `--sandbox`, `--fields`, and `--expect-snapshot` before the command. See the [command reference](commands.md) for every option and bound.

## Find pages and stored recognition text

Use `info`, `ls`, or `tree` to find a notebook. Use `pages` for its current one-based page numbers. Select notebooks by exact UUID, full path, or unique exact name; use the full path or UUID when a name is ambiguous.

`pages --with-recognition` adds per-page metadata without returning candidate text. `recognition.availability` distinguishes `stored-text`, `no-stored-text`, and `unavailable`; `recognition.index_state` distinguishes `pending`, `not-pending`, and `unknown`. Counts describe stored recognition rows and characters, not ink strokes or transcription accuracy. The page `annotation_count` comes from `projection.notes_items`; it is not an ink-stroke or recognition count.

Read candidate text with `read --source handwriting`. Preserve the source label, raw delimiters, and warnings. Label any cleaned reconstruction as an interpretation rather than a verbatim quote or visual transcription. A pending-index warning can appear even when no stored candidate text is returned. `not-pending` does not verify recognition accuracy or freshness. `pdf` text may come from the index or a mapped PDF background in the copy. `textbox` is typed text stored on the page.

## Search and check coverage

Use `search --notebook NOTEBOOK` for one notebook or `search --folder FOLDER` for a subtree. These options are mutually exclusive. If both are omitted, the scope is `/`. Search checks stored text-index rows only. Read `coverage_detail`, `warnings`, and source labels before interpreting an empty result. The reported counts and page-metadata diagnostics describe stored rows in the resolved scope; they do not prove that every page was indexed. Completeness remains `unknown`, including when metadata appears complete. `next_steps` suggests follow-up commands; search does not start a PDF scan automatically.

When a result may be in a copied PDF background, run `scan-pdfs` separately with the same notebook or folder scope. It does not search handwriting, the stored index, or a PDF registered with `attach-pdf`. `next_page_offset` counts scanned pages, not hits. Continue with the same query, scope, and snapshot. `scan_complete` means the scoped page sequence is exhausted. `coverage_complete` is true only when the current scan starts at offset 0, reaches the end, and has no extraction gaps. Check `stop_reason` and warnings for a hit limit, page limit, missing background, missing PDF mapping, unreadable PDF text, or truncated extraction. Even complete PDF-background coverage says nothing about handwriting.

## Keep results comparable

For a paginated sequence, read the `snapshot` ID from its cache-backed result and pass it before every later command as `--expect-snapshot ID`. Follow `next_offset` with `--offset`, or `next_page_offset` with `--page-offset` for `scan-pdfs`. If the CLI returns `stale_snapshot`, restart the sequence against the new view. Do not combine results from different snapshot IDs. The `snapshot` value from `sync` or the standalone `snapshot` command is a path; use `snapshot_id` from `sync` or a generation ID from a cache-backed read result.

## Project fields and interpret output

Use `uv run goodnotes schema COMMAND` to discover row projections and valid fields. Row-based command schemas include an output projection and a `--fields` example. For example, `uv run goodnotes --fields number,recognition pages "Notebook name" --with-recognition` keeps page numbers and recognition metadata. Add `--with-recognition` whenever selecting `recognition`. For search hits, `--fields path,page,source,text` keeps the source label with the text. Field projection retains surrounding response metadata, including warnings, pagination, and `snapshot`; use keys valid for the selected row level.

Successful data responses are compact JSON by default. `--text` only indents it. Expected runtime errors are JSON on stderr with exit code `1`; invalid command syntax and parser-checked bounds exit `2`. Page-expression and other application-checked errors are runtime errors. An interrupt or broken pipe exits `130`. Read stderr instead of treating empty stdout as a result.

## Use exports carefully

`export --pages EXPR` selects distinct one-based pages in the listed order; omitting it exports all pages. It has no 20-page cap. `render`, `export`, and `bundle` require new destinations outside the snapshot and production Goodnotes locations. The default `background` layer does not overlay native ink. An `annotated` layer requires a user-supplied PDF registered with `attach-pdf`; the CLI checks the file and page count but cannot verify annotation inclusion or page order. Preserve `annotation_status: "user-provided-unverified"` and warnings with any conclusion.

For source and annotation details, see [PDFs and handwriting](pdf-and-handwriting.md). For development and checks, see [Contributing](../CONTRIBUTING.md).
