# Command reference

Generated from the registered Typer commands and their machine-readable schema. After changing the CLI, update this page with `uv run --locked python scripts/docs.py`.

Run the CLI as `uv run goodnotes`. Put global options before the command. Use `uv run goodnotes COMMAND --help` for terminal help and `uv run goodnotes schema COMMAND` for the complete machine-readable contract.

For interpreting fields, warnings, and pagination, see [results and output](output.md) and the [agent guide](agent-guide.md).

## Command index

| Command | Syntax |
| --- | --- |
| [`sync`](#sync) | `goodnotes sync [OPTIONS]` |
| [`watch`](#watch) | `goodnotes watch [OPTIONS]` |
| [`snapshot`](#snapshot) | `goodnotes snapshot [OPTIONS] LIBRARY DESTINATION` |
| [`ls`](#ls) | `goodnotes ls [OPTIONS]` |
| [`tree`](#tree) | `goodnotes tree [OPTIONS]` |
| [`info`](#info) | `goodnotes info [OPTIONS] NOTEBOOK` |
| [`pages`](#pages) | `goodnotes pages [OPTIONS] NOTEBOOK` |
| [`read`](#read) | `goodnotes read [OPTIONS] NOTEBOOK` |
| [`search`](#search) | `goodnotes search [OPTIONS] QUERY` |
| [`scan-pdfs`](#scan-pdfs) | `goodnotes scan-pdfs [OPTIONS] QUERY` |
| [`export`](#export) | `goodnotes export [OPTIONS] NOTEBOOK` |
| [`render`](#render) | `goodnotes render [OPTIONS] NOTEBOOK` |
| [`bundle`](#bundle) | `goodnotes bundle [OPTIONS] NOTEBOOK` |
| [`attach-pdf`](#attach-pdf) | `goodnotes attach-pdf [OPTIONS] NOTEBOOK PDF` |
| [`doctor`](#doctor) | `goodnotes doctor [OPTIONS]` |
| [`schema`](#schema) | `goodnotes schema [OPTIONS] [COMMAND]` |

## Global options

Accepted before a command:

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--sandbox` | Path | Unset; env `GOODNOTES_SANDBOX` | Use an existing read view. |
| `--data-dir` | Path | Unset; env `GOODNOTES_DATA_DIR` | Managed cache directory; use the same selection for sync and reads. |
| `--text` | Flag | `False` | Indent JSON for inspection. |
| `--json` | Flag | `False` | Compact JSON, the default. |
| `--verbose` | Flag | `False` | Include full metadata and native format diagnostics. |
| `--fields` | Text | Unset | Comma-separated row fields or response keys; see schema COMMAND output. |
| `--expect-snapshot` | Text | Unset | Reject a changed pagination generation. |
| `--version` | Flag | `False` | Print the version. |

`--fields` is unset by default, so omitting it uses the command's default response profile. Supplying it selects keys from the full response contract; consult `schema COMMAND` for the command-specific fields. Compact JSON is the default output format; the schema reports the `--json` flag's parser default separately.

## Commands

### sync

```text
goodnotes sync [OPTIONS]
```

Create or refresh the managed library copy.

Reads source files without opening source SQLite or writing to Goodnotes. Reuses
an unchanged copy unless --force is set. Later source edits require another sync.
Requires cache write access and initializes persistent locks for read-only readers.

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--library` | Path | Unset | Goodnotes Library directory. |
| `--force` / `--no-force` | Flag | `False` | Rebuild and verify even when files appear unchanged. |

Example:

```sh
goodnotes sync --library /path/to/Library
```

Output details: [`uv run goodnotes schema sync`](#schema) reports the fields and output profile.


### watch

```text
goodnotes watch [OPTIONS]
```

Keep the copied library refreshed while this foreground process runs.

Emits one JSON object per ready, updated, recovery, or error event. Unchanged
checks stay quiet. Ctrl-C stops the watcher. This observes files on the Mac;
it cannot establish whether the latest iPad changes have reached this Mac.

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--library` | Path | Unset | Goodnotes Library directory. |
| `--interval` | Number | `2` | Seconds between metadata checks. Bounds: 0.05 to 3600. |
| `--debounce` | Number | `1` | Seconds to coalesce edits before capture. Bounds: 0 to 60. |
| `--iterations` | Integer | Unset | Stop after this many polling iterations. Bounds: at least 1. |

Example:

```sh
goodnotes watch --library /path/to/Library --interval 2
```

Output details: [`uv run goodnotes schema watch`](#schema) reports the fields and output profile.


### snapshot

```text
goodnotes snapshot [OPTIONS] LIBRARY DESTINATION
```

Create an independent copy at a new destination.

LIBRARY must contain Databases/ and Attachments/. Does not change the active cache.
Read the result with the global --sandbox option.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `LIBRARY` | Path | Required | Source Library directory. |
| `DESTINATION` | Path | Required | New snapshot directory to create. |

Example:

```sh
goodnotes snapshot /path/to/Library /path/to/saved-copy
```

Output details: [`uv run goodnotes schema snapshot`](#schema) reports the fields and output profile.


### ls

```text
goodnotes ls [OPTIONS]
```

List a folder's direct children.

--folder accepts a path, UUID, or unique name; / selects the root. Continue with
next_offset and pin the returned snapshot ID using global --expect-snapshot.

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--folder` | Text | `/` | Folder UUID, path, or unique exact name; / is the root. |
| `--limit` | Integer | `100` | Bounds: 1 to 1000. |
| `--offset` | Integer | `0` | Bounds: at least 0. |

Example:

```sh
goodnotes ls --folder "Course" --limit 20
```

Output details: [`uv run goodnotes schema ls`](#schema) reports the fields and output profile.


### tree

```text
goodnotes tree [OPTIONS]
```

List descendants within a depth and result limit.

--folder selects the starting folder, / by default. Continue with next_offset;
global --expect-snapshot keeps pagination on one generation.

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--folder` | Text | `/` | Folder UUID, path, or unique exact name; / is the root. |
| `--depth` | Integer | `3` | Bounds: 1 to 50. |
| `--limit` | Integer | `100` | Bounds: 1 to 1000. |
| `--offset` | Integer | `0` | Bounds: at least 0. |

Example:

```sh
goodnotes tree --folder "Course" --depth 2
```

Output details: [`uv run goodnotes schema tree`](#schema) reports the fields and output profile.


### info

```text
goodnotes info [OPTIONS] NOTEBOOK
```

Show a folder or notebook's metadata.

NOTEBOOK accepts an exact UUID, full library path, or unique exact name.
Ambiguous names fail; use a full path or UUID to disambiguate.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |

Example:

```sh
goodnotes info "Course Notes"
```

Output details: [`uv run goodnotes schema info`](#schema) reports the fields and output profile.


### pages

```text
goodnotes pages [OPTIONS] NOTEBOOK
```

List pages in current notebook order, numbered from 1.

Returns pages and next_offset. --with-recognition adds metadata without reading
candidate text. annotation_count counts projected note items, not ink or recognition.
Continue with --offset and pin --expect-snapshot for a consistent generation.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--limit` | Integer | `100` | Bounds: 1 to 1000. |
| `--offset` | Integer | `0` | Bounds: at least 0. |
| `--with-recognition` / `--no-with-recognition` | Flag | `False` | Include stored handwriting recognition availability and index state. |

Example:

```sh
goodnotes pages "Course Notes" --limit 20
```

Output details: [`uv run goodnotes schema pages`](#schema) reports the fields and output profile.


### read

```text
goodnotes read [OPTIONS] NOTEBOOK
```

Read page text with PDF, handwriting, and textbox source labels.

Defaults to page 1. Choose --page or --pages, up to 20 distinct pages. The
character budget covers all selected pages; check truncated in the response.
Handwriting is stored recognition output. Native ink and attached PDFs are not decoded.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--page` | Integer | Unset | Bounds: at least 1. |
| `--pages` | Text | Unset | Up to 20 pages, e.g. 1-3,7. |
| `--source` | Text | `all` | all, pdf, handwriting, or textbox. |
| `--max-chars` | Integer | `12000` | Bounds: 1 to 1000000. |

Example:

```sh
goodnotes read "Course Notes" --pages 1-3,7 --source handwriting
```

Output details: [`uv run goodnotes schema read`](#schema) reports the fields and output profile.


### search

```text
goodnotes search [OPTIONS] QUERY
```

Search stored text for a literal, case-insensitive phrase.

Choose --notebook for one notebook or --folder for a subtree. coverage_detail
reports scoped source availability, not proof that every page was indexed.
No hits do not prove absence; scan-pdfs searches PDF backgrounds separately.
Snippets are capped at 320 characters.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `QUERY` | Text | Required | Literal, case-insensitive text to find. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--folder` | Text | Unset | Folder selector; defaults to /. Use with --notebook omitted. |
| `--notebook` | Text | Unset | Exact notebook UUID, path, or unique name; excludes --folder. |
| `--source` | Text | `all` | all, pdf, handwriting, or textbox. |
| `--limit` | Integer | `20` | Bounds: 1 to 100. |
| `--offset` | Integer | `0` | Bounds: at least 0. |

Example:

```sh
goodnotes search "regression" --folder "Course" --limit 10
```

Output details: [`uv run goodnotes schema search`](#schema) reports the fields and output profile.


### scan-pdfs

```text
goodnotes scan-pdfs [OPTIONS] QUERY
```

Search PDF background text when the stored index misses it.

Choose --notebook or --folder. Continue next_page_offset with --page-offset,
the same scope, query, and snapshot. coverage_complete requires a complete
scan from offset 0 without extraction gaps. Does not search handwriting or attached PDFs.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `QUERY` | Text | Required | Literal, case-insensitive PDF text to find. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--folder` | Text | Unset | Folder selector; defaults to /. Use with --notebook omitted. |
| `--notebook` | Text | Unset | Exact notebook UUID, path, or unique name; excludes --folder. |
| `--limit` | Integer | `20` | Bounds: 1 to 100. |
| `--page-limit` | Integer | `200` | Bounds: 1 to 250. |
| `--page-offset` | Integer | `0` | Bounds: at least 0. |

Example:

```sh
goodnotes scan-pdfs "regression" --folder "Course" --page-limit 50
```

Output details: [`uv run goodnotes schema scan-pdfs`](#schema) reports the fields and output profile.


### export

```text
goodnotes export [OPTIONS] NOTEBOOK
```

Export all pages, or selected pages in listed order, to a new PDF.

The default background layer omits native ink. The annotated layer requires
attach-pdf and uses an unverified user export. Existing outputs are never replaced.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--output` | Path | Required | New PDF file to write. |
| `--layer` | Text | `background` | background, native, or annotated. |
| `--pages` | Text | Unset | Selected pages in listed order, e.g. 1-3,7. Defaults to all. |

Example:

```sh
goodnotes export "Course Notes" --output /tmp/course.pdf
```

Output details: [`uv run goodnotes schema export`](#schema) reports the fields and output profile.

For temporary cache behavior and persistent output paths, see [temporary and saved files](output.md#temporary-and-saved-files).


### render

```text
goodnotes render [OPTIONS] NOTEBOOK
```

Render one page to a temporary cached PNG, or save it with --output.

--layer native reconstructs supported copied ink and reports coverage. Background
omits native ink; annotated requires attach-pdf. Page numbers start at 1.
Cached images expire after a day of inactivity and share a 100 MB limit.
Returned paths are protected for five minutes. Explicit outputs are never replaced.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--output` | Path | Unset | Save a persistent PNG outside the cache. |
| `--page` | Integer | `1` | Bounds: at least 1. |
| `--layer` | Text | `background` |  |
| `--dpi` | Integer | `150` | Bounds: 72 to 300. |

Example:

```sh
goodnotes render "Course Notes" --page 3 --output /tmp/page-3.png
```

Output details: [`uv run goodnotes schema render`](#schema) reports the fields and output profile.

For temporary cache behavior and persistent output paths, see [temporary and saved files](output.md#temporary-and-saved-files).


### bundle

```text
goodnotes bundle [OPTIONS] NOTEBOOK
```

Inspect pages using a temporary cached bundle, or save it with --output.

Select up to 10 distinct pages with --pages, e.g. 1-3,7. One character budget
covers all text. Defaults to native ink images and PDF text. Native reconstruction
reports coverage; annotated requires attach-pdf. Text keeps its original source labels.
--source filters returned text only; the image still uses the selected layer.
Returns page evidence directly. A bundle with at least one rendered native image keeps
text for failed pages and reports their render errors; a bundle with no rendered images
remains an error. Global --verbose includes full metadata.
Cached outputs share a 100 MB limit and expire after a day of inactivity.
Returned paths are protected for five minutes; explicit outputs remain until removed.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--output` | Path | Unset | Save a persistent bundle outside the cache. |
| `--pages` | Text | `1` |  |
| `--layer` | Text | `native` |  |
| `--dpi` | Integer | `150` | Bounds: 72 to 300. |
| `--max-chars` | Integer | `12000` | Bounds: 1 to 1000000. |
| `--source` | Text | `pdf` | pdf, all, handwriting, or textbox text. |

Example:

```sh
goodnotes bundle "Course Notes" --pages 1-3 --output /tmp/course-pages
```

Output details: [`uv run goodnotes schema bundle`](#schema) reports the fields and output profile.

For temporary cache behavior and persistent output paths, see [temporary and saved files](output.md#temporary-and-saved-files).


### attach-pdf

```text
goodnotes attach-pdf [OPTIONS] NOTEBOOK PDF
```

Register a Goodnotes-exported PDF for annotated rendering and export.

Checks file integrity and page count, not annotation inclusion or page order.
Keep the PDF unchanged at its path. Re-register after a new snapshot generation.
Does not add the PDF's text to read, search, or scan-pdfs.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `NOTEBOOK` | Text | Required | Exact UUID, full library path, or unique exact name. |
| `PDF` | Path | Required | Existing PDF exported from Goodnotes. |

Example:

```sh
goodnotes attach-pdf "Course Notes" /path/to/annotated.pdf
```

Output details: [`uv run goodnotes schema attach-pdf`](#schema) reports the fields and output profile.


### doctor

```text
goodnotes doctor [OPTIONS]
```

Report local library counts, missing metadata, and text-source warnings.

storage contains cache_dir, snapshot_path, and captured_at; unknown values are null.
Reads the selected copy without refreshing it or contacting Goodnotes.

**Options**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `--access-only` / `--no-access-only` | Flag | `False` | Check filesystem access without opening databases or writing files. |
| `--library` | Path | Unset | Optional source Library to check with --access-only. |
| `--scratch-dir` | Path | Unset | Native scratch directory to check with --access-only. |
| `--output-dir` | Path | Unset | Output directory to check with --access-only. |

Example:

```sh
goodnotes doctor
```

Output details: [`uv run goodnotes schema doctor`](#schema) reports the fields and output profile.


### schema

```text
goodnotes schema [OPTIONS] [COMMAND]
```

Show the command index or one command's machine-readable contract.

Does not load notes or refresh the library. Targeted details include registered
arguments, options, defaults, bounds, global options, and usage notes.

**Arguments**

| Name | Type | Default | Description and bounds |
| --- | --- | --- | --- |
| `COMMAND-NAME` | Text | Unset | Command to describe in detail. |

Example:

```sh
goodnotes schema read
```

## Artifact retention

Temporary render and bundle artifacts follow bounded cache retention; explicit output paths are persistent. See [temporary and saved files](output.md#temporary-and-saved-files) for cleanup and output-path details.
