# Everyday tasks

These recipes assume you have [installed the CLI and synced a library](getting-started.md).
Run commands from the checkout. Replace `"Course Notes"` and example paths with your own values.

## Find a notebook and page

```sh
uv run --locked goodnotes --text ls --limit 20
uv run --locked goodnotes --text ls --folder "Courses" --limit 20
uv run --locked goodnotes --text pages "Course Notes" --limit 20
```

`ls` lists direct children. Use `tree --depth 2` to explore nested folders.
Choose a notebook's exact name, full path, or ID. Use the full path or ID when names repeat.
Pages follow current notebook order, which does not establish which page was edited most recently.
For a known notebook and page, go straight to `read`, `render`, or `bundle`.

To locate a lecture topic, use the [search recipe](#search-notes-and-pdf-backgrounds) below
and inspect the hit's page. Follow [pagination](output.md#pagination-and-consistent-reads) for longer lists.

## Inspect a page with an agent

You need the [optional native rendering runtime](pdf-and-handwriting.md#native-ink-without-manual-pdf-exports)
for reconstructed handwriting. Refresh once, then request the pages you need:

```sh
uv run --locked goodnotes sync
uv run --locked goodnotes bundle "Course Notes" --pages 3
```

For an explicit Library folder, use `sync --library /path/to/Library`.
A restricted agent can skip sync and use a copy maintained by another process.

Open `page_evidence[].image` and read the inline text. By default, bundle combines
native images with PDF text. Add `--source all` if you also need stored handwriting
candidates and typed text. This changes text selection only.

Check warnings and native coverage before describing handwriting. If a multi-page
bundle returns `status: "partial"`, inspect `failed_pages`; those rows retain text fields, which may be empty,
but have no image. See the [agent guide](agent-guide.md) for evidence handling.

For clean backgrounds without a native runtime:

```sh
uv run --locked goodnotes bundle "Course Notes" --pages 1-3 --layer background
```

One bundle holds a single read view. For later matching reads, use its `snapshot`
as [a generation guard](output.md#pagination-and-consistent-reads).

## Read text or stored handwriting candidates

```sh
uv run --locked goodnotes --text read "Course Notes" --pages 1-3,7 --source pdf
uv run --locked goodnotes --text read "Course Notes" --page 3 --source handwriting
```

`read` defaults to all available text sources. It accepts up to 20 pages and
shares its default 12,000-character budget across them. Check `truncated`;
increase `--max-chars` or request fewer pages if needed.

Stored handwriting candidates can contain alternatives and raw delimiters.
They are not a verified transcription. Quote only exact stored text and label
any cleaned reading as interpretation.

To find pages with stored recognition text or pending indexing:

```sh
uv run --locked goodnotes --fields number,recognition pages "Course Notes" --with-recognition
```

This reports metadata, not ink or candidate text. `no-stored-text` does not mean
there is no handwriting. See [recognition states](pdf-and-handwriting.md#find-stored-handwriting-recognition).

## Search notes and PDF backgrounds

Start with a scoped stored-index search:

```sh
uv run --locked goodnotes --text search "regression" --notebook "Course Notes"
```

Each hit identifies its page and text source. Search matches literal,
case-insensitive substrings. Check coverage and warnings even if there are no hits.
Use `--folder "Courses"` instead of `--notebook` to search a folder.

If the index misses a phrase, or search fails because its index is unavailable,
scan copied PDF backgrounds for slide text:

```sh
uv run --locked goodnotes --text scan-pdfs "regression" --notebook "Course Notes"
```

| Command | Searches | Does not search |
| --- | --- | --- |
| `search` | Stored index rows for PDF, handwriting candidates, and typed text | Unindexed PDF contents or images |
| `scan-pdfs` | Text extracted from copied PDF backgrounds | Native handwriting, image OCR, or registered annotated PDFs |

Search never starts a PDF scan automatically. A scan checks at most 200 pages by
default, with a maximum `--page-limit` of 250. Continue with `next_page_offset`
as `--page-offset`, keeping the query, scope, and snapshot unchanged.
`scan_complete` means the page sequence ended. Only `coverage_complete: true`
also confirms a scan from offset 0 with no extraction gaps.

## Export selected pages or a notebook

You need a new writable output path. These commands export backgrounds without native ink:

```sh
uv run --locked goodnotes export "Course Notes" --pages 1-3,7 --output /tmp/selected-pages.pdf
uv run --locked goodnotes export "Course Notes" --output /tmp/notebook-backgrounds.pdf
uv run --locked goodnotes render "Course Notes" --page 3 --output /tmp/page-3-background.png
```

Page expressions preserve the listed order and reject duplicates. Omitting `--pages`
exports all pages. Export has no 20-page cap.

To include supported native ink, add `--layer native` after
[setting up its runtime](pdf-and-handwriting.md#native-ink-without-manual-pdf-exports).
For the app's own rendering, follow [the exported-PDF workflow](pdf-and-handwriting.md#use-a-pdf-exported-by-goodnotes)
and use `--layer annotated`.

## Keep a bundle after the session

```sh
uv run --locked goodnotes bundle "Course Notes" --pages 3 --output /tmp/saved-study-page
```

This creates page PNGs and `text.json` in a new directory. Native setup is required
for the default layer. Use `--layer background` when you want clean page backgrounds.
The CLI never overwrites an existing destination.
Without `--output`, render and bundle paths are [temporary](output.md#temporary-and-saved-files).

For continuous updates or cache-only agent access, see [access and refresh](access.md).
