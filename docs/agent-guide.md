# Agent guide

Use the installed `goodnotes` executable, or `uv run goodnotes` from the repository. Global options go before the command. Consult `schema COMMAND` or `COMMAND --help` for unfamiliar options; skip discovery calls when the notebook path and page are known.

## Inspect a recent page

1. Refresh once at the start of the request, using the configured source and cache:

   ```sh
   goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
   ```

2. Request the page evidence:

   ```sh
   goodnotes --data-dir /path/to/cli-cache bundle "Course/Lectures/Notebook name" --pages 3 --output /tmp/study-page-3
   ```

3. Read the inline source-labeled text and coverage, then open the absolute `image` path in `page_evidence`. No separate manifest-read call is needed. Use a new output directory each time. The full diagnostic manifest remains at `manifest` for investigation.

Bundle defaults to PDF text plus a native annotated image, and returns the evidence directly. This avoids redundant OCR candidates. Use `--source all` for all stored text sources, including recognition and typed text. The source filter affects text only. `--layer background` explicitly requests a clean page without native ink; missing native support never causes a silent fallback.

A known path and page can go directly to `read`, `render`, or `bundle`; skip `info` and `pages`. If the location is unknown, use `ls --folder FOLDER --limit 20` in the smallest known folder and follow pagination. Selectors accept an exact UUID, full library path, or unique exact name. Ambiguous names require a full path or UUID.

One bundle holds a single read view, so this workflow needs no snapshot-ID argument. For additional reads or pagination that must match, pass the returned `snapshot` before later commands as `--expect-snapshot ID`. On `stale_snapshot`, restart the sequence. Do not sync again between matching reads. Document and page UUIDs come from Goodnotes and remain stable across ordinary CLI refreshes. A changed capture gets a new snapshot generation ID; unchanged sync keeps it. Reuse known notebook selectors across requests.

Sync observes files already on the Mac. It cannot confirm that a particular iPad edit has arrived. `captured_at` alone does not prove the expected note is present; compare the image with that edit. If refresh fails, report the failure instead of presenting an old copy as current.

## Interpret page evidence

Use the image as the visual source for handwriting. Native reconstruction is approximate. Preserve `annotation_status`, `image_source`, native coverage, `full_fidelity_verified`, object counts, unsupported reasons, and warnings when assessing the notes. `includes_handwriting: true` means supported strokes rendered; it does not establish completeness. Missing native data means unknown availability. Never substitute a clean background and describe it as the complete notes. Standalone `render` and `export` retain their background defaults; use `--layer native` with those commands for ink.

Text parts retain `source`, engine, priority, and truncation. `handwriting` is stored recognition output with raw candidate delimiters, not a visual transcription. Quote only exact raw substrings; label cleaned wording as interpretation. `pdf` text can come from the stored index or a copied background. `textbox` is stored typed text. See [PDFs and handwriting](pdf-and-handwriting.md) for source and layer limits.

## Keep output small

Routine responses are concise by default. They retain interpretation limits, pagination, and truncation. Use global `--verbose` when you need full metadata; bundle manifests always retain full render diagnostics. Errors are complete. `schema COMMAND` describes both response shapes.

Use `--max-chars` to bound read/bundle text for the question. Inspect `truncated` before concluding that text is absent. Concise output does not reduce source text or image size. Request only the needed pages. For custom selection, `--fields` projects the full field contract instead of the concise profile; row projection retains sibling warnings, pagination, and snapshot metadata.

## Follow the relevant branch

- **Search.** `search` covers stored index rows; `scan-pdfs` separately covers copied PDF backgrounds. Neither establishes handwriting completeness. Check coverage and warnings before interpreting no hits. Follow `next_offset` or `next_page_offset` with the same scope, query, and snapshot. See [search commands](commands.md#search).
- **Recognition metadata.** `pages --with-recognition` reports stored-text availability and pending/unknown index state. `annotation_count` counts projected note items, not ink or OCR. See [pages](commands.md#pages).
- **Cache access or refresh failures.** Setup/sync needs source read and cache write access. Readers need only cache read access, including the existing `.view.lock`; native rendering also needs writable scratch and output. Read `error.details` for the path, operation, and remedy. Do not bypass locks or silently select another cache. `doctor --access-only` checks permissions without writes or engines. See [restricted agent setup](usage.md#read-access-for-an-agent).
- **Continuous refresh.** `watch` is an optional foreground refresher for the selected cache, useful for cache-only readers. It observes Mac files, and readers still pin each request's generation. See [watch](commands.md#watch).
- **Other output or errors.** Commands use compact JSON; `--text` indents it. Runtime errors go to stderr with exit 1, invalid syntax exits 2, interruption exits 130. Read stderr when stdout is empty. See [command reference](commands.md).
