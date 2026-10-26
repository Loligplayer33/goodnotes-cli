# Command reference

The CLI owns its option, default, bound, and output-field reference. Look them up
without loading notes:

```sh
uv run --locked goodnotes --help
uv run --locked goodnotes read --help
uv run --locked goodnotes schema read
```

## Commands

| Command | Purpose |
| --- | --- |
| `sync` | Create or refresh the managed copy. `--force` bypasses unchanged-file reuse. |
| `watch` | Refresh continuously in the foreground. |
| `snapshot LIBRARY DESTINATION` | Create a separate marked copy at a new destination. |
| `ls`, `tree` | List direct children or descendants of a folder. |
| `info NOTEBOOK` | Resolve an exact ID, full path, or unique exact name. |
| `pages NOTEBOOK` | List pages in current order; `--with-recognition` adds stored-index metadata. |
| `read NOTEBOOK` | Return source-labelled text. |
| `search QUERY` | Search the stored text index. |
| `scan-pdfs QUERY` | Search copied PDF backgrounds. |
| `render NOTEBOOK` | Save one page as PNG. |
| `export NOTEBOOK` | Export all or selected pages as PDF; `--output` is required. |
| `bundle NOTEBOOK` | Return page images and text together. |
| `attach-pdf NOTEBOOK PDF` | Register a full-notebook Goodnotes PDF export. |
| `doctor` | Inspect the selected copy; `--access-only` checks paths without database opens or writes. |
| `schema [COMMAND]` | Print the machine-readable command contract. |

## Results

Global options go before the command. Compact JSON is the default;
`--text` indents it and explicit `--json` takes precedence.
`--verbose` includes full metadata. `--fields` selects from the full contract;
for row arrays it preserves sibling warnings, pagination, and snapshot metadata.
Use `schema COMMAND` for valid keys and conditional fields.

Text parts keep `source: pdf`, `handwriting`, or `textbox`. Preserve warnings and
unknown/null values. `truncated` means the character budget cut requested text.
Read accepts up to 20 pages and bundle up to 10, each with a shared default
12,000-character budget. Export has no 20-page cap. See [image layers](pdf-and-handwriting.md).

## Pagination

Use `next_offset` as `--offset` for listings/search; use `next_page_offset` as
`--page-offset` for PDF scans. Keep query, scope, and other options unchanged.
Pin the first read's `snapshot` on subsequent calls:

```sh
uv run --locked goodnotes ls --limit 20
uv run --locked goodnotes --expect-snapshot SNAPSHOT_ID ls --limit 20 --offset NEXT_OFFSET
```

Replace the placeholders with returned values. On `stale_snapshot`, restart against
the new generation. One bundle already holds one view across images and text.
Sync returns the generation as `snapshot_id`; its verbose `snapshot` is a path.
The standalone snapshot command also returns a path in `snapshot`.

PDF scans check at most 200 pages by default, up to 250 per call. Their cursor counts
pages scanned, not hits. `scan_complete` means the page sequence ended;
`coverage_complete` also requires starting at offset 0 with no extraction gaps.

## Temporary and saved files

Render and bundle without `--output` use temporary storage under `artifacts/` in
the selected data directory. It retains up to 100,000,000 logical bytes, counting
identical PNGs once. Entries expire after 24 hours since last use; cleanup runs on
later artifact operations. A five-minute lease protects returned paths. Capacity
cleanup can remove older entries before 24 hours; protected entries can cause
`artifact_cache_capacity`. Repeating a request uses the current copy.

Explicit outputs are caller-owned, persistent, and never pruned. Use a new path
outside the source Library and snapshots. Existing files/directories are not overwritten.

## Errors

Operation errors are complete JSON on stderr with exit 1. Invalid syntax or parser
bounds exit 2; application-checked values such as invalid page expressions exit 1.
Interruption or a closed pipe exits 130. Successful completion still requires checking
warnings, coverage, and partial status. Watch emits JSON events, including retry errors,
on stdout. Help and version output are plain text.
