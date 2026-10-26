# Command reference

The main help lists everyday commands. The schema index exposes advanced commands
in its `advanced_commands` field; each remains available through targeted
`goodnotes COMMAND --help` and `goodnotes schema COMMAND`. The annotated-PDF path
remains available for Goodnotes exports that native reconstruction cannot reproduce.

## Output and sources

Compact JSON is the default; `--text` indents it for inspection. `--verbose`
includes full metadata, and `--fields` selects response keys or row fields from the
schema. Warnings, source labels, unknown values, and pagination metadata remain
available unless the selected fields omit them.

`read`, `search`, and `bundle` default to `--source text` (PDF text and typed
textboxes). `--source all` or `--source handwriting` explicitly requests stored
handwriting-recognition candidates. The CLI does not run image OCR. `pages` always
includes stored-recognition counts and index state; the hidden `--with-recognition`
option remains accepted as a compatibility alias.

`bundle` returns images and text, defaults to native images, and accepts up to 10
pages. `read` accepts up to 20 pages. Both use a shared default 12,000-character
budget across their selected pages. Use `--pages` for one or more pages; `read
--page` remains a hidden compatibility alias. Text parts keep their `source` and
`truncated` fields.

`render` and `export` default to the native layer to include supported ink. Use
`--layer background` for an ink-free background. `--layer annotated` uses a
registered Goodnotes PDF. Native output is approximate and may fail when the native
runtime or copied data is unavailable. Check page-level coverage and warnings.

## Pagination and refresh status

Use `next_offset` as `--offset` for listings and search. PDF scans use
`next_page_offset` with `--page-offset`. Keep the other query and scope options
unchanged. Pin the first response's `snapshot` on subsequent calls:

```sh
goodnotes ls --limit 20
goodnotes --expect-snapshot SNAPSHOT_ID ls --limit 20 --offset NEXT_OFFSET
```

On `stale_snapshot`, restart against the new generation. A bundle already holds
one read view. Sync returns `snapshot_id`, `checked_at`, and `captured_at`.
`source_changed_during_refresh` describes only whether the source inventory changed
during that refresh; `false` does not mean there were no edits since the previous
sync. An `unchanged` status means the prior snapshot was reused, so its
`captured_at` can precede `checked_at`.

## Saved files and errors

Render and bundle without `--output` use temporary artifact storage. Entries expire
after 24 hours since last use, subject to the 100,000,000-byte logical limit; a
five-minute lease protects returned paths. Explicit outputs are caller-owned and
are never overwritten or pruned. Keep them outside the source Library and
snapshots.

Bundle may return partial page images when some pages succeed; failed rows have
`image: null` and `render_error`. Full PDF export is atomic: if any selected page
fails, no partial PDF is published and the error retains page context. Native
failures do not silently fall back to the background.

Operation errors are JSON on stderr with exit 1. Invalid syntax or parser bounds
exit 2; interruption or a closed pipe exits 130. Check warnings and coverage even
after successful completion.
