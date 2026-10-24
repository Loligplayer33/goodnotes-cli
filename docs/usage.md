# Usage guide

Run the commands below from the project directory with `uv run goodnotes`. This keeps the CLI and its dependencies in the project's environment. See the [command reference](commands.md) for every option and limit.

## 1. Create or refresh the local copy

On macOS, sync discovers the Goodnotes library and prepares the local read view:

```sh
uv run goodnotes sync
```

On another system, give `sync` a path to an available copy of the `Library` folder:

```sh
uv run goodnotes sync --library /path/to/Library
```

The folder must contain `Databases/` and `Attachments/`. Run `sync` again after Goodnotes changes the library. To rebuild the copy even when its saved file inventory appears unchanged, add `--force`.

To make a separate copy at a path you manage, use `snapshot`, then pass that copy with the global `--sandbox` option:

```sh
uv run goodnotes snapshot /path/to/Library /path/to/saved-copy
uv run goodnotes --sandbox /path/to/saved-copy ls
```

## 2. Find a notebook

List items at the library root, or show folders and notebooks below it:

```sh
uv run goodnotes --text ls
uv run goodnotes --text tree --depth 2
```

Use the notebook name, path, or ID with `info`, then list its pages:

```sh
uv run goodnotes --text info "Notebook name"
uv run goodnotes --text pages "Notebook name"
```

If a name matches more than one notebook, use its full path or ID. Page numbers start at 1 and follow the notebook's current order.

## 3. Read page text

Read the first page or choose a page range:

```sh
uv run goodnotes --text read "Notebook name" --page 1
uv run goodnotes --text read "Notebook name" --pages 1-3,7
```

`read` labels text sources separately. Use `--source pdf`, `--source handwriting`, or `--source textbox` to select one source. Handwriting results are recognition candidates stored by Goodnotes. They do not show how the ink looks on the page. The default text budget is 12,000 characters across the selected pages; use `--max-chars` to change it. The result reports when text was truncated.

## 4. Search for a phrase

Search uses the stored text index:

```sh
uv run goodnotes --text search "lecture topic"
uv run goodnotes --text search "lecture topic" --folder "Course"
```

If the index misses text that appears in a copied PDF background, scan those PDF pages separately:

```sh
uv run goodnotes --text scan-pdfs "lecture topic" --page-limit 200
```

`scan-pdfs` searches PDF backgrounds only. It does not search handwriting or the stored index. If more pages remain, use the returned `next_page_offset` as `--page-offset` on the next call.

## 5. Export a page or notebook

Export a notebook to PDF, render one page to PNG, or create a small set of page images and text data:

```sh
uv run goodnotes export "Notebook name" --output /tmp/notebook.pdf
uv run goodnotes render "Notebook name" --page 3 --output /tmp/page-3.png
uv run goodnotes bundle "Notebook name" --pages 1-3 --output /tmp/notebook-pages
```

These commands use the `background` layer by default. It contains each page's source attachment and does not add native Goodnotes ink. `bundle` writes one PNG per selected page and a `text.json` manifest. It accepts up to 10 pages and applies one character budget across their text. For annotated PDF output, follow [PDFs and handwriting](pdf-and-handwriting.md).

Output paths must be new. The CLI does not replace an existing file or directory.

## 6. Continue a long listing

List commands return a bounded page of results and a `next_offset` when more items remain. Use that number as `--offset`:

```sh
uv run goodnotes --text ls --limit 100 --offset 100
```

If you need all pages of a listing to come from the same local copy, pass the first result's `snapshot` value as the global `--expect-snapshot` option on later requests. Global options go before the command:

```sh
uv run goodnotes --expect-snapshot SNAPSHOT_ID ls --limit 100 --offset 100
```

If the local copy changed, the CLI returns `stale_snapshot`. Restart the listing using the new snapshot value instead of combining results from different copies.

## 7. Use help and the built-in command contract

Show the full command list or one command's options:

```sh
uv run goodnotes --help
uv run goodnotes read --help
```

For a machine-readable command index or details for one command, use `schema` with or without a command name. Neither form loads notebook data:

```sh
uv run goodnotes schema
uv run goodnotes schema read
```

The CLI emits JSON by default. `--text` indents that JSON; `--json` keeps it compact. Global options such as `--data-dir`, `--fields`, `--text`, and `--expect-snapshot` go before the command name. See the [agent guide](agent-guide.md) for operational guidance and the [command reference](commands.md) for all flags.
