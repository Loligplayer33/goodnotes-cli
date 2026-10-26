# Agent guide

Use the installed `goodnotes` executable or `uv run --locked goodnotes` from the
checkout. Consult `schema COMMAND` for unfamiliar options. Global options go first.

## Request page evidence

With source access, sync once at the start of the request, then request only needed pages:

```sh
goodnotes sync --library /path/to/Library
goodnotes bundle "Course Notes" --pages 3
```

Bundle defaults to native images and PDF text. Uncached native pages need the
[native runtime](pdf-and-handwriting.md#native-setup) and writable scratch. Add `--source all` for stored recognition and typed text. Read inline
`page_evidence`, then open each absolute `image` path; the full manifest is only
needed for deeper diagnostics. Use a new `--output` for persistent evidence.

For known selectors/pages, skip discovery. Otherwise list the smallest known folder
and follow cursors. A cache-only reader uses a copy maintained by another process;
see [access setup](usage.md#set-up-a-restricted-agent-or-refresh-continuously).
Report refresh failures instead of presenting an old copy as current.

## Interpret the evidence

| Evidence | Interpretation |
| --- | --- |
| Native coverage, unsupported reasons, warnings, `full_fidelity_verified` | Preserve limits. Native output is approximate; zero unsupported objects does not prove completeness. |
| `rendered_strokes`, `rendered_paths`, `rendered_images` | Strokes include supported pen marks and filled paths. Images do not establish handwriting. |
| Missing native data | Availability is unknown. A background is not complete note evidence. |
| Image body field 6 absent or decoder-unverified JPEG | Keep the appearance/integrity warning and partial coverage. |
| Text `source` and `truncated` | Preserve PDF/handwriting/textbox labels and budget limits. Handwriting is stored candidate text, not a verified visual transcription. |
| Recognition or `annotation_count` | Stored-index state/projected note-item counts, not proof of visible ink. |

For a partial native bundle, inspect `failed_pages`. Failed rows retain text fields,
which may be empty, but have `image: null` and `render_error`. Keep successful-page
warnings too. Single-page/all-page image failures remain errors. Global engine,
snapshot-integrity, safety, and publication failures are fatal; individual unavailable
image objects may instead produce partial coverage. No background fallback occurs.

## Keep reads consistent and bounded

Reuse known document/page IDs across ordinary refreshes. Page positions can change.
Use the returned generation with `--expect-snapshot ID` for later matching reads
or pagination; restart on `stale_snapshot`. Do not sync mid-sequence.
A bundle already holds one read view. See [cursors and output](commands.md).

Sync sees files already on the Mac. Capture time cannot prove an expected iPad edit
arrived; inspect the image. Search covers the stored index; scan copied PDF
backgrounds separately when needed. Check coverage before concluding no text exists.
Request only needed pages, set `--max-chars`, and check truncation. For extra metadata,
use `--verbose` or `--fields`. Read stderr when stdout is empty.
