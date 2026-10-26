# Agent guide

Use the installed `goodnotes` executable, or `uv run goodnotes` from the repository. Global options go before the command. Consult `schema COMMAND` or `COMMAND --help` for unfamiliar options; skip discovery calls when the notebook path and page are known.

## Inspect a recent page

1. Refresh once at the start of the request, using the configured source and cache:

   ```sh
   goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
   ```

2. Request the page evidence:

   ```sh
   goodnotes --data-dir /path/to/cli-cache bundle "Course/Lectures/Notebook name" --pages 3
   ```

3. Read the inline source-labeled text and coverage, then open the absolute `image` path in `page_evidence`. No separate manifest-read call is needed. The full diagnostic manifest is at `manifest` for investigation.

Without `--output`, renders and bundles return `temporary: true`. Matching requests reuse page images, and bundle text always comes from the current pinned read view. The separate `artifacts/` directory under the selected data directory is capped at 100,000,000 logical bytes, counting shared PNGs only once. Entries expire after one day since their last CLI use; cleanup happens on the next artifact operation. Returned paths are protected for five minutes, then may be evicted for capacity before the day ends. Open them promptly. If a path has expired, repeat the command; this produces evidence for the currently selected view, not a historical version. Add `--output` only when a persistent copy is needed. Explicit outputs remain caller-owned and require a new destination each time.

Bundle defaults to PDF text plus native annotated page images, and returns the evidence directly. This avoids redundant OCR candidates. Use `--source all` for all stored text sources, including recognition and typed text. The source filter affects text only. If a multi-page native bundle has both rendered pages and page-local native failures, inspect top-level `status` and `failed_pages`. A failed row keeps its text, has `image: null`, and includes `render_error`; it has no rendered image metadata. Do not interpret that row as a clean page. Retry the affected page after correcting or refreshing the copied data. A single-page or all-page failure remains an error. `--layer background` explicitly requests a clean page without native ink; the CLI never substitutes it for failed native output.

A known path and page can go directly to `read`, `render`, or `bundle`; skip `info` and `pages`. If the location is unknown, use `ls --folder FOLDER --limit 20` in the smallest known folder and follow pagination. Selectors accept an exact UUID, full library path, or unique exact name. Ambiguous names require a full path or UUID.

One bundle holds a single read view, so this workflow needs no snapshot-ID argument. For additional reads or pagination that must match, pass the returned `snapshot` before later commands as `--expect-snapshot ID`. On `stale_snapshot`, restart the sequence. Do not sync again between matching reads. Document and page UUIDs come from Goodnotes and remain stable across ordinary CLI refreshes. A changed capture gets a new snapshot generation ID; unchanged sync keeps it. Reuse known notebook selectors across requests.

Sync observes files already on the Mac. It cannot confirm that a particular iPad edit has arrived. `captured_at` alone does not prove the expected note is present; compare the image with that edit. If refresh fails, report the failure instead of presenting an old copy as current.

## Interpret page evidence

Use the image as the visual source for handwriting. Native reconstruction is approximate. Preserve `annotation_status`, `image_source`, native coverage, `full_fidelity_verified`, object counts, unsupported reasons, and warnings when assessing the notes. `rendered_strokes` includes supported pen marks, filled paths, and the observed open-line shape; `rendered_paths` and `rendered_images` are separate counts. Native images do not set `includes_handwriting`. When `native_image_body_field_6_absent_records` is nonzero, keep its warning: the image is rendered, but the field's effect on application appearance or visibility is unverified and coverage remains partial. A zero `unsupported_objects` count does not establish completeness because opaque fields and drawing order may remain unverified. Missing native data means unknown availability. Never substitute a clean background and describe it as the complete notes. Standalone `render` and `export` retain their background defaults; use `--layer native` with those commands for supported native annotations.

Text parts retain `source`, engine, priority, and truncation. `handwriting` is stored recognition output with raw candidate delimiters, not a visual transcription. Quote only exact raw substrings; label cleaned wording as interpretation. `pdf` text can come from the stored index or a copied background. `textbox` is stored typed text. See [PDFs and handwriting](pdf-and-handwriting.md) for source and layer limits.

## Keep output small

Routine responses are concise by default. They retain interpretation limits, pagination, and truncation. Use global `--verbose` when you need full metadata; bundle manifests always retain full render diagnostics. Errors are complete. `schema COMMAND` describes both response shapes.

Use `--max-chars` to bound read/bundle text for the question. Inspect `truncated` before concluding that text is absent. Concise output does not reduce source text or image size. Request only the needed pages. For custom selection, `--fields` projects the full field contract instead of the concise profile; row projection retains sibling warnings, pagination, and snapshot metadata.

## Follow the relevant branch

- **Search.** `search` covers stored index rows; `scan-pdfs` separately covers copied PDF backgrounds. Neither establishes handwriting completeness. Check coverage and warnings before interpreting no hits. Follow `next_offset` or `next_page_offset` with the same scope, query, and snapshot. See [search commands](commands.md#search).
- **Recognition metadata.** `pages --with-recognition` reports stored-text availability and pending/unknown index state. `annotation_count` counts projected note items, not ink or OCR. See [pages](commands.md#pages).
- **Cache access or refresh failures.** Setup/sync needs source read and cache write access. Metadata and text readers need only cache read access, including the existing `.view.lock`; native rendering also needs writable scratch. Default visual commands need writable artifact storage. With a read-only cache, use `--output` in a writable directory. Read `error.details` for the path, operation, and remedy. Do not bypass locks or silently select another cache. `doctor --access-only` checks permissions without writes or engines and includes an optional artifact-storage write hint. See [restricted agent setup](usage.md#read-access-for-an-agent).
- **Continuous refresh.** `watch` is an optional foreground refresher for the selected cache, useful for cache-only readers. It observes Mac files, and readers still pin each request's generation. See [watch](commands.md#watch).
- **Other output or errors.** Commands use compact JSON; `--text` indents it. Runtime errors go to stderr with exit 1, invalid syntax exits 2, interruption exits 130. Read stderr when stdout is empty. See [command reference](commands.md).
