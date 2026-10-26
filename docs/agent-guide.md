# Agent guide

Use the installed `goodnotes` executable or `uv run --locked goodnotes` from the
checkout. The main help lists everyday commands. Use `goodnotes schema COMMAND`
or `goodnotes COMMAND --help` for command details; global options go first.

## Read for an overview, bundle for visuals

With approved source access, refresh once at the start of a request. Then read a
small page range for a text overview and bundle the pages that need visual review:

```sh
goodnotes sync --library /path/to/Library
goodnotes read "Course Notes" --pages 1-5
goodnotes bundle "Course Notes" --pages 3,5
```

`read` and `bundle` default to `--source text`, which includes PDF text and typed
textboxes. Use `--source all` or `--source handwriting` to request stored
handwriting recognition candidates. This is stored text, not a new OCR pass.
Bundle defaults to native page images; open each `page_evidence[].image` and use
the visual evidence when explaining or teaching from the page. The full manifest
is only needed for deeper diagnostics. Use a new `--output` for persistent files.

For known pages, skip discovery. Otherwise list the smallest known folder and
follow its cursor. `pages` includes recognition counts and index state by default;
use them to prioritize review, not to exclude pages. The index can miss diagrams,
images, unindexed ink, or other visual content. `annotation_count` counts projected
note items, not visible ink.

## Interpret page evidence

| Evidence | Interpretation |
| --- | --- |
| Native coverage, unsupported reasons, warnings, `full_fidelity_verified` | Preserve the limits. Native reconstruction is approximate, and zero unsupported objects does not prove completeness. |
| `native-no-records-observed` / `no-native-records-observed` | The copied store returned no page records. The background image is still available, but the warning says absence is unverified; do not conclude that the page has no ink. |
| `rendered_strokes`, `rendered_paths`, `rendered_images` | Strokes include supported pen marks and filled paths. Images do not establish handwriting. |
| Missing native data or a native error | Availability is unknown. Store, runtime, and read failures remain errors; no background fallback hides them. |
| Text `source` and `truncated` | Keep PDF, handwriting, and textbox labels and note every budget cut. Handwriting is stored candidate text, not a verified transcription. |
| Recognition or `annotation_count` | Recognition describes the stored index; `annotation_count` describes projected note items. Neither proves visible ink. |

For a partial native bundle, inspect `failed_pages`. Failed rows keep their text
fields, which may be empty, and have `image: null` with `render_error`. Preserve
warnings on successful pages too. A bundle can be partial when some images succeed;
if every image fails, the operation fails. Full PDF export is all-or-nothing and
retains page context on failure.

## Keep reads consistent and bounded

Reuse known document/page IDs across refreshes. Page positions can change. Use the
returned generation with `--expect-snapshot ID` for later reads or pagination;
restart on `stale_snapshot`. Do not sync mid-sequence. A bundle already holds one
read view.

Sync sees files on the Mac; its catalog cannot establish item provenance or prove
that an expected iPad edit arrived. `doctor` counts describe the selected copy and
do not establish data loss or source-index completeness. See the
[refresh status semantics](commands.md#pagination-and-refresh-status) before
interpreting sync fields.

Search covers the stored index; `scan-pdfs` is the advanced fallback for copied PDF
backgrounds. Neither runs image OCR. Check coverage and warnings before treating no
hits as absence. Read stderr when stdout is empty. Use `--verbose` or `--fields`
when extra metadata is needed.
