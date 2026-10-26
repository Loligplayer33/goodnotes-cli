# Everyday tasks

[Install and sync](../README.md) first. Replace the example names, paths, and page
numbers with your own. Use an exact ID or full path when names repeat. See
[command results](commands.md#output-and-sources).

## Find and read pages

```sh
uv run --locked goodnotes --text ls --folder "Courses" --limit 20
uv run --locked goodnotes --text pages "Course Notes" --limit 20
uv run --locked goodnotes --text read "Course Notes" --pages 1-3
```

Use `ls --kind folder` or `ls --kind notebook` to filter before pagination.

`read` defaults to `--source text`: indexed PDF text plus typed textboxes, without
stored handwriting-recognition candidates. Use `--source all` to include all
sources or `--source handwriting` to read candidates alone. Page numbers follow
current notebook order, not edit recency. `--pages` accepts a comma-separated
range or list; public page numbers start at 1.

`pages` includes recognition counts and index state. Use them to prioritize pages
for review, not as a test for whether a page has useful visuals. The index may miss
diagrams, images, or unindexed ink. `doctor` counts and index warnings describe
the selected local copy; they do not establish data loss. The root catalog shows
local library membership, not item authorship, import source, or device/cloud provenance.
Use `doctor --notebook "Course Notes"` to separate that notebook's page-index counts
from library-wide catalog diagnostics.

## Inspect pages visually

Read a short range for a rough text overview, then bundle only the pages that need
visual explanation:

```sh
uv run --locked goodnotes read "Course Notes" --pages 1-5
uv run --locked goodnotes bundle "Course Notes" --pages 3,5
```

Bundle defaults to native images and `--source text`. Open each
`page_evidence[].image` and use the image as evidence when explaining the page.
Recognition counts can help prioritize review, but do not exclude pages based on
empty counts. For stored candidates, add `--source all` or choose
`--source handwriting`; the CLI does not run image OCR. Check coverage and warnings
before drawing conclusions. See the [agent guide](agent-guide.md) for partial-page
behavior and snapshot consistency.

## Search stored text

```sh
uv run --locked goodnotes --text search "regression" --notebook "Course Notes"
uv run --locked goodnotes --text search "regression" --notebook "Course Notes" --source handwriting
```

Search defaults to PDF and typed text. Add `--source all` or `--source handwriting`
to search stored recognition candidates. Search returns bounded snippets; no hits do
not prove absence. The advanced `scan-pdfs` command checks copied PDF backgrounds
when the stored index misses slide text. Neither search path runs image OCR or
searches registered annotated PDFs. Inspect a hit with `bundle --pages PAGE`.

## Save visual evidence

`export` writes a PDF of selected pages or the whole notebook. It defaults to the
native layer, which includes supported reconstructed ink; use `--layer background`
for an ink-free background. `bundle --output NEW_DIRECTORY` saves page PNGs and a
text manifest. `render` is the advanced single-PNG command. Native rendering needs
the [optional runtime](pdf-and-handwriting.md#native-setup), reports coverage, and
does not guarantee full fidelity. The advanced annotated-PDF path uses a
Goodnotes-exported PDF; see [images and handwriting](pdf-and-handwriting.md).

Page expressions preserve listed order and reject duplicates. Destinations must be
new; existing files and directories are not overwritten. Render and bundle without
`--output` use [temporary artifact storage](commands.md#saved-files-and-errors).

## Restricted readers and refresh

Run setup with source read and cache write access, then give the reader access to
the executable and prepared cache:

```sh
uv run --locked goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
.venv/bin/goodnotes --data-dir /path/to/cli-cache doctor
.venv/bin/goodnotes --data-dir /path/to/cli-cache ls
```

Metadata and text readers need cache read access, including its existing lock.
Managed images need artifact-cache write access; explicit outputs can use another
writable location. Native rendering also needs the runtime and writable scratch.

Keep one snapshot generation across reads that must match. Sync sees files already
on this Mac; capture time cannot prove an expected iPad edit arrived. See the
[refresh status semantics](commands.md#pagination-and-refresh-status) when checking
`source_changed_during_refresh`, `status`, `captured_at`, and `checked_at`.

`--data-dir` overrides `GOODNOTES_DATA_DIR`; the default is
`$XDG_CACHE_HOME/goodnotes-cli`, or `~/.cache/goodnotes-cli`. The advanced
`snapshot` command creates a separate marked copy for use with global `--sandbox`.
The advanced `watch` command refreshes in the foreground while it runs. Neither
can confirm that an expected iPad edit has reached the Mac.

## Recover from common failures

| Symptom | Next step |
| --- | --- |
| Access failure | Run `doctor --access-only`; read `details.path`, `operation`, and `remedy`. Access checks create nothing; write checks are hints. |
| Uninitialized cache or missing lock | Stop other CLI processes, then sync the selected cache. Never delete or replace active locks. |
| Native image or runtime failure | Check the selected layer and [native requirements](pdf-and-handwriting.md#native-setup). Errors stay visible; the CLI does not substitute a background. |
| Empty or cut-off text | Check source availability, warnings, and `truncated`; request fewer pages or raise `--max-chars`. |
| Missing temporary image or full artifact cache | Repeat the request, retry after protected entries expire, or save with `--output`. |
| Annotated PDF no longer matches its registration | Register the matching full PDF again. |
