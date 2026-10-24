# Goodnotes CLI

A local, read-only command line tool for browsing a Goodnotes library. On macOS, `sync` discovers Goodnotes' local library and copies its files into the CLI cache. Other commands read the active copy to list notebooks, inspect pages, search stored text, and work with PDF pages. The CLI does not modify Goodnotes or its source library.

The project requires Python 3.11 or newer. It uses Typer for commands, PyMuPDF for PDF work, Python's standard SQLite support for library data, and `uv` to manage dependencies in the project `.venv`.

## Install

From the repository directory, run:

```sh
uv sync
uv run goodnotes --help
```

`uv sync` creates the project environment. Run project commands through `uv run`; there is no global install step.

## Sync Goodnotes

After installing the project, create or refresh the CLI's local copy:

```sh
uv run goodnotes sync
uv run goodnotes ls
```

`sync` discovers the macOS Goodnotes library, checks its file inventory and active cache metadata, and reuses the current copy when both match. If either changed, it makes an independent copy, validates it, and updates the CLI's active cache. On supported APFS volumes, copy-on-write clones reduce copying; other filesystems use ordinary independent copies. The CLI reads source files but never opens source SQLite databases or controls the Goodnotes app. Other commands use only copied databases in read-only mode. Run `sync` after Goodnotes changes the library to refresh the CLI's view. There is no background refresh.

Use `uv run goodnotes sync --force` to rebuild and verify a fresh copy even when the metadata checks match. If the source changes during a capture attempt, `sync` retries and keeps the previous view if capture cannot succeed. `source_changed_during_refresh` compares source inventories taken before and after capture, including retries; when true, `sync` publishes the validated view with `captured_at` and a warning that it is valid as of that time. `false` means those inventories matched, not that the source stayed unchanged through return. Updates after the final check, including while waiting to publish, are detected by the next `sync`.

The default cache is `~/.cache/goodnotes-cli`, based on `XDG_CACHE_HOME`. Set `GOODNOTES_DATA_DIR` or pass the global `--data-dir` option to choose another cache location. To select a library directory explicitly, pass `--library` to `sync`; it must contain `Databases/` and `Attachments/`:

```sh
uv run goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
```

## Browse and search

Once synced, commands use the active cache without a sandbox option. Pass a library notebook name or path where the command expects `NOTEBOOK`:

```sh
uv run goodnotes doctor
uv run goodnotes --text ls --limit 100
uv run goodnotes tree --depth 2
uv run goodnotes pages "Course Notes"
uv run goodnotes read "Course Notes" --page 14
uv run goodnotes read "Course Notes" --pages 1-4 --source handwriting --max-chars 12000
uv run goodnotes search "lecture" --source all
uv run goodnotes scan-pdfs "lecture" --folder "Course" --page-limit 200 --limit 20
uv run goodnotes --fields name,path ls
```

Commands return compact JSON by default. Add the global `--text` option before the command to indent JSON for inspection. `--json` explicitly selects compact JSON. Global options such as `--data-dir`, `--fields`, `--text`, and `--expect-snapshot` go before the command name. List and tree results are bounded to 100 items by default. Use `--limit` and `--offset` to page through larger results. `--fields name,path` selects fields in list, page, and search result rows. Search performs literal, case-insensitive substring matching and returns up to 20 snippets by default. It reports `coverage: stored-index` and warnings when an indexed text source or page mapping is missing.

Browse results include the active snapshot ID as `snapshot`. Pass it back with the global `--expect-snapshot` option on later paginated requests. If a sync has published a different snapshot, the CLI returns a `stale_snapshot` error so you can restart pagination against one consistent view:

```sh
uv run goodnotes --expect-snapshot SNAPSHOT_ID ls --limit 100 --offset 100
```

If indexed search misses text that is present in a copied PDF background, use `scan-pdfs`. It scans up to 200 pages by default, accepts `--page-limit` up to 250, and returns `next_page_offset` when more pages remain. Pass that value to `--page-offset` to continue. PDF scanning does not search handwriting or the stored index.

Public page numbers start at 1. `read` defaults to page 1, accepts up to 20 requested pages per call, and caps total text at 12,000 characters by default. Set `--max-chars` to choose another cap. The result marks truncated content. `schema` prints the command contract without reading notebook data.

Indented JSON keeps source labels so a reader can distinguish PDF text, stored handwriting recognition candidates, and typed text boxes. `read` uses indexed PDF text when available. If it is absent, it extracts text from the matching PDF background in the copied library. These routes do not decode native annotations; text flattened into an imported PDF may still be extracted. Image-only writing does not produce PDF text. Recognition candidates do not reproduce handwritten ink visually.

## Export and render

The `background` layer follows each page's source attachment in the notebook's current order:

```sh
uv run goodnotes export "Course Notes" --output /tmp/course.pdf --layer background
uv run goodnotes render "Course Notes" --page 14 --output /tmp/page.png --layer background
uv run goodnotes bundle "Course Notes" --pages 1-4 --max-chars 12000 --output /tmp/course-pages --layer background
uv run goodnotes attach-pdf "Course Notes" /path/to/course-annotated.pdf
uv run goodnotes export "Course Notes" --output /tmp/course-annotated.pdf --layer annotated
```

`bundle` writes up to 10 page images and source-labeled text in `text.json`. Its 12,000-character default budget covers text across all selected pages; `--max-chars` changes the budget, and the result reports whether text was truncated. The `background` layer exports each page's available PDF background in notebook order. It does not overlay native Goodnotes ink. Marks already flattened into a source PDF remain part of that background.

The CLI does not decode native Goodnotes ink. For an annotated layer, first export the notebook as a PDF from Goodnotes with both **Include Annotation** and **Include Page Background** enabled, then register that PDF with `attach-pdf`. Registration checks the PDF and page count, but cannot verify that annotations were included or that the PDF page order matches the notebook. Annotated render and export results report `includes_handwriting: null` and `annotation_status: user-provided-unverified`; they do not claim that ink is present. The CLI stores the PDF path and checks the file before use, so keep the PDF at that path and do not modify it after registration.

Run `uv run goodnotes --help` or a subcommand's `--help` for all current options.

Refreshes publish a new read view only after validation. A command keeps using the view it opened, even while a refresh copies changed files. The final pointer update and cleanup wait for current readers to finish. The managed cache keeps the active generation; named snapshots made with `snapshot LIBRARY DESTINATION` remain at the requested destination when stored outside the managed cache.

## Project notes

- [Architecture](docs/architecture.md) describes package boundaries and the cache safety rules.
- [Agent guide](docs/agent-guide.md) covers safe development and private data handling.
- [PDF and handwriting](docs/pdf-and-handwriting.md) explains background exports and annotated-PDF limits.
- [Robustness](docs/robustness.md) maps concurrent updates, incomplete data, and output safeguards to their documented behavior.
- [Contributing](CONTRIBUTING.md) lists setup and local checks.
- [Releases](docs/releases.md) describes the tagged release process.
