# Agent guide

Use `uv run goodnotes` from the repository root. Start with `uv run goodnotes schema [COMMAND]` for the machine-readable contract or `uv run goodnotes COMMAND --help` for human-readable options. Put global options such as `--sandbox`, `--fields`, and `--expect-snapshot` before the command. See the [command reference](commands.md) for every command, option, default, and bound.

## Choose a way to inspect the notebook

1. Use `info`, `ls`, or `tree` to find a notebook. Use `pages` to get its current one-based page numbers. Select by exact UUID, full path, or unique exact name. If a name is ambiguous, use a full path or UUID.
2. Use `read` for text from selected pages. Keep the `source` labels and warnings with the text. `handwriting` contains Goodnotes recognition candidates, not a visual transcription of ink. `pdf` text may come from the search index or a mapped PDF background in the copy. `textbox` is typed page text.
3. Use `search` for a literal, case-insensitive search of the stored index. Check `coverage` and `warnings` before treating no hits as evidence that text is absent. If the index may be incomplete, use `scan-pdfs` to search copied PDF backgrounds. That scan does not search handwriting, the stored index, or a PDF registered with `attach-pdf`. Continue while `next_page_offset` is present, and account for warnings about missing, unmapped, unreadable, or truncated pages.
4. For visual inspection, use `render` for one page or `bundle` for up to ten pages. The default `background` layer shows PDF backgrounds and does not overlay native Goodnotes ink. An `annotated` layer requires a PDF supplied by the user and registered with `attach-pdf`. Registration is tied to the current snapshot generation. The CLI verifies the file and page count, but cannot verify annotation inclusion or page order; preserve `annotation_status: "user-provided-unverified"` and the warnings with any conclusion.

## Keep results comparable

For a paginated sequence, read the `snapshot` ID from its cache-backed result and pass it before every command as `--expect-snapshot ID`. Follow `next_offset` with `--offset`, or `next_page_offset` with `--page-offset` for `scan-pdfs`. If the CLI returns `stale_snapshot`, restart the sequence against the new view. Do not combine pages from different snapshot IDs. The `snapshot` value returned by `sync` or the standalone `snapshot` command is a path; use `snapshot_id` from `sync` if needed, or the generation ID from a cache-backed read result.

Use `--fields` only with keys present at the selected output level. It filters object rows under `items`, `pages`, or `hits`, including empty row arrays, while preserving surrounding metadata. Otherwise it filters top-level keys. Cache-backed commands always retain `snapshot`. Keep source labels whenever they affect the strength of a conclusion.

## Handle output and failures

Successful data responses are JSON on stdout. `--text` only indents the JSON. Expected runtime errors are JSON on stderr and exit with code `1`; invalid command syntax and parser-checked bounds exit with code `2`. Page-expression and other application-checked errors are runtime errors. An interrupt or broken pipe exits with code `130`. Read stderr for the error message instead of treating an empty stdout as a result.

Choose a new destination for `export`, `render`, and `bundle`; the CLI refuses to overwrite existing outputs, and output paths must stay outside the snapshot and production Goodnotes locations. `sync` refreshes the managed cache, `snapshot` creates an independent copy, and `attach-pdf` writes registration metadata beside the snapshot. These commands have different effects; consult the [command reference](commands.md) before using them.

For source and annotation details, see [PDF and handwriting](pdf-and-handwriting.md). For development and tests, see [Contributing](../CONTRIBUTING.md).
