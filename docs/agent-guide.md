# Agent guide

Use the installed `goodnotes` executable, or `uv run --locked goodnotes` from the
checkout. Global options go before the command. Use `schema COMMAND` for unfamiliar
options; skip discovery calls when you already know the notebook and page.

## Inspect a recent page

1. Refresh once at the start of the request if you have source access:

   ```sh
   goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
   ```

2. Request just the needed page evidence:

   ```sh
   goodnotes --data-dir /path/to/cli-cache bundle "Course Notes" --pages 3
   ```

3. Read inline text and warnings, then open `page_evidence[].image`. Full diagnostics
   are available at `manifest`; a separate manifest read is usually unnecessary.

Bundle defaults to native images and PDF text. It needs the [native runtime](pdf-and-handwriting.md#native-ink-without-manual-pdf-exports)
when rendering uncached native pages. Add `--source all` to include stored recognition
and typed text. A cache-only agent uses a copy maintained by another process;
see [access setup](access.md). A refresh failure must be reported, not presented as current data.

| Situation | Next step |
| --- | --- |
| Unknown location | `ls --folder FOLDER --limit 20` in the smallest known folder; follow cursors. |
| Ambiguous name | Use an exact full path or ID. |
| Later reads must match | Pass bundle's `snapshot` with global `--expect-snapshot ID`; do not sync again mid-sequence. |
| `stale_snapshot` | Restart against the new generation. |
| Need a persistent image or bundle | Supply a new `--output` path. Temporary paths may expire after their five-minute protection period. |
| Read-only cache | Use explicit writable output; native reconstruction also needs writable scratch. |

Sync sees files already on the Mac. Capture time does not prove an expected iPad
edit is present. Compare the image with the edit. Reuse known document/page IDs
across ordinary refreshes; a snapshot generation ID changes on a new capture.

## Interpret page evidence

Use the image to assess visible handwriting. Native reconstruction remains approximate.
Keep these distinctions in your answer:

| Evidence | Interpretation |
| --- | --- |
| Native `coverage`, unsupported reasons, warnings, `full_fidelity_verified` | Preserve limits. Zero unsupported objects does not prove completeness; opaque fields and drawing order may remain unverified. |
| `rendered_strokes`, `rendered_paths`, `rendered_images` | Counts differ. Strokes include supported pen marks and filled paths; images do not establish handwriting. |
| Missing native data | Availability is unknown. Do not describe a background as complete notes. |
| Native image field 6 absent or decoder-unverified JPEG | Keep the appearance/integrity warning and partial coverage. |
| Text `source: handwriting` | Raw stored recognition candidates. Quote exact substrings only; label cleaned wording as interpretation. |
| Text `source: pdf` / `textbox` | Copied or indexed PDF text / stored typed text. Preserve source labels and truncation. |

For `status: "partial"`, check `failed_pages`. A failed row retains text fields, which may be empty, but has
`image: null` and `render_error`. It is not evidence of a clean page. Successful
rows retain their own warnings. Single-page/all-page failures remain errors;
no background fallback occurs.

## Keep requests small and complete

| Task | Rule |
| --- | --- |
| Read or bundle | Request only needed pages and use `--max-chars`; check `truncated`. |
| Search | `search` uses the stored index; `scan-pdfs` separately extracts copied PDF backgrounds. Check coverage before concluding no text exists. |
| Recognition status | `pages --with-recognition` reports stored text/index state, not ink; `annotation_count` counts projected note items. |
| Pagination | Follow `next_offset` or `next_page_offset` with unchanged query, scope, and snapshot. |
| More diagnostics | Use `--verbose` or the full manifest. `--fields` selects from the full contract. |
| Empty stdout | Inspect stderr and exit status. Operation errors exit 1, parser errors 2, interruptions 130. |

See [results and output](output.md) for response shapes, retention, and cursors;
[troubleshooting](troubleshooting.md) for recovery steps.
