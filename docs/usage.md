# Everyday tasks

[Install and sync](../README.md) first. Replace example names, paths, and pages with
your own. Use exact full paths or IDs when names repeat. See [result conventions](commands.md#results).

## Find and read a page

```sh
uv run --locked goodnotes --text ls --folder "Courses" --limit 20
uv run --locked goodnotes --text pages "Course Notes" --limit 20
uv run --locked goodnotes --text read "Course Notes" --pages 1-3 --source pdf
```

Omit `--folder` to list the root; use `tree --depth 2` to explore nested folders.
Pages follow current notebook order, which does not establish edit recency.
For a known notebook/page, go straight to read, render, or bundle.
`read` defaults to all text sources. Select `pdf`, `handwriting`, or `textbox` as needed.

To locate stored handwriting candidates:

```sh
uv run --locked goodnotes --fields number,recognition pages "Course Notes" --with-recognition
uv run --locked goodnotes read "Course Notes" --page 3 --source handwriting
```

Recognition metadata describes the stored index, not visible ink. A cleaned reading
of raw candidates is an interpretation. See [recognition states](pdf-and-handwriting.md#stored-recognition).

## Inspect a page with an agent

After [native setup](pdf-and-handwriting.md#native-setup), refresh once and request the needed page:

```sh
uv run --locked goodnotes sync
uv run --locked goodnotes bundle "Course Notes" --pages 3
```

Use `sync --library /path/to/Library` for an explicit source. Open
`page_evidence[].image` and read the inline PDF text. Add `--source all` for stored
recognition and typed text; it changes text selection only. For clean backgrounds
without the native runtime, use `bundle --layer background`.
Check warnings and [partial results](agent-guide.md#interpret-the-evidence) before drawing conclusions.

## Search notes and PDF backgrounds

```sh
uv run --locked goodnotes --text search "regression" --notebook "Course Notes"
uv run --locked goodnotes --text scan-pdfs "regression" --notebook "Course Notes"
```

| Command | Searches |
| --- | --- |
| `search` | Literal, case-insensitive substrings in stored index rows. Use `--folder "Courses"` instead for folder scope. |
| `scan-pdfs` | Text extracted from copied PDF backgrounds, independently of the index. |

Use the second command when the index misses slide text or is unavailable.
Neither command runs image OCR or searches registered annotated PDFs. PDF scans
exclude native handwriting. Search never launches a scan automatically.
Check coverage and warnings before treating no hits as absence. Use a hit's one-based
`page` in `bundle --pages PAGE` to inspect it; pages are not listed by latest edit.

## Export or save page images

These examples use backgrounds without native ink:

```sh
uv run --locked goodnotes export "Course Notes" --pages 1-3,7 --output /tmp/selected-pages.pdf
uv run --locked goodnotes render "Course Notes" --page 3 --output /tmp/page-3.png
uv run --locked goodnotes bundle "Course Notes" --pages 1-3 --layer background --output /tmp/study-pages
```

Page expressions preserve listed order and reject duplicates. Omit export's
`--pages` to include the whole notebook. Destinations must be new; nothing is overwritten.
A bundle saves page PNGs and `text.json`. Render and bundle without `--output`
use [temporary storage](commands.md#temporary-and-saved-files).
For handwriting, choose the [native or annotated layer](pdf-and-handwriting.md).

## Set up a restricted agent or refresh continuously

Run setup with source read and cache write access, then give the reader access to
the executable and prepared cache through its runner:

```sh
uv run --locked goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
.venv/bin/goodnotes --data-dir /path/to/cli-cache doctor
.venv/bin/goodnotes --data-dir /path/to/cli-cache ls
```

Use an absolute executable path outside the checkout. Metadata/text readers need
only cache read access, including the existing lock. Managed visual outputs need
artifact-cache write access; explicit `--output` can use another writable location.
Uncached native rendering also needs runtime access and writable scratch; `attach-pdf` needs registration storage write access.

`--data-dir` overrides `GOODNOTES_DATA_DIR`; the default is
`$XDG_CACHE_HOME/goodnotes-cli`, or `~/.cache/goodnotes-cli`.
For a caller-owned copy, use `snapshot LIBRARY NEW_DESTINATION`, then global
`--sandbox NEW_DESTINATION`. A raw Library folder is not a marked snapshot.

For continuous refresh, run `watch --library /path/to/Library` in a separate terminal
with the same data directory. It runs in the foreground, stays quiet while unchanged,
and stops with Ctrl-C. Sync/watch see files on the Mac; they cannot confirm an iPad edit arrived.
Do not refresh between [reads that must match](commands.md#pagination).

## Recover from common failures

| Symptom | Next step |
| --- | --- |
| Access failure | Run `doctor --access-only`, optionally with `--library PATH`, `--scratch-dir PATH`, or `--output-dir PATH`. Read `details.path`, `operation`, and `remedy`. Checks create nothing; write checks are hints. |
| Uninitialized cache or missing lock | Stop other CLI processes, then sync the selected cache. Never delete or replace active locks. |
| Missing handwriting in an image | Check the requested layer and [native requirements](pdf-and-handwriting.md#native-setup). Missing native data is unknown, not proof of a blank page. |
| Empty or cut-off text | Check source availability, warnings, and `truncated`; request fewer pages or increase `--max-chars`. |
| Missing temporary image or full artifact cache | Repeat the request, retry after protected entries expire, or save with `--output`. |
| Annotated PDF fails after refresh or a file edit | Register the matching full PDF again. |
