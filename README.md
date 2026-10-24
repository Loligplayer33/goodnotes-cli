# Goodnotes CLI

Goodnotes CLI lets you browse, search, read, and export a local Goodnotes library from the command line. It reads a managed copy of the library and does not edit Goodnotes or its source files.

Live library discovery is for macOS. On Linux, point `sync --library` at an available copy of a Goodnotes `Library` folder that contains `Databases/` and `Attachments/`.

## What it can do

| Task | Support | Limit |
| --- | --- | --- |
| Browse notebooks and pages | Yes | Reads the last synced copy. Run `sync` again after Goodnotes changes the library. |
| Search and read text | Yes | `search` checks the stored text index. `read` returns PDF text, stored handwriting recognition candidates, and typed text. Use `scan-pdfs` explicitly for copied PDF backgrounds. The CLI does not OCR images or reconstruct ink. |
| View and export page backgrounds | Yes | Uses each page's PDF or image background. Native Goodnotes ink is not included. |
| View and export annotations | With a supplied PDF | You must export and register a PDF from Goodnotes. The CLI cannot verify that it includes annotations or that its page order matches. |
| Edit Goodnotes notebooks | No | The CLI does not write to the Goodnotes library. |

## Commands

The complete option reference is in [Command reference](docs/commands.md). Each command also has built-in `--help`.

| Group | Command | Purpose |
| --- | --- | --- |
| Setup | [`sync`](docs/commands.md#sync) | Create or refresh the managed copy. |
| Setup | [`snapshot`](docs/commands.md#snapshot) | Save a separate copy of a Library folder. |
| Setup | [`doctor`](docs/commands.md#doctor) | Check the current copy and available text sources. |
| Setup | [`schema`](docs/commands.md#schema) | Print the machine-readable command contract without loading notes. |
| Browse | [`ls`](docs/commands.md#ls) | List a folder's direct children. |
| Browse | [`tree`](docs/commands.md#tree) | List folders and notebooks below a folder. |
| Browse | [`info`](docs/commands.md#info) | Resolve a notebook name, path, or ID. |
| Browse | [`pages`](docs/commands.md#pages) | List pages in their current notebook order. |
| Read | [`read`](docs/commands.md#read) | Read text for one or more pages. |
| Read | [`search`](docs/commands.md#search) | Find text in the stored search index. |
| Read | [`scan-pdfs`](docs/commands.md#scan-pdfs) | Search PDF backgrounds when the index misses text. |
| Export | [`export`](docs/commands.md#export) | Export notebook pages to a PDF. |
| Export | [`render`](docs/commands.md#render) | Save one page as a PNG image. |
| Export | [`bundle`](docs/commands.md#bundle) | Save page images with their text data. |
| Export | [`attach-pdf`](docs/commands.md#attach-pdf) | Register a Goodnotes-exported PDF for annotated output. |

## Quickstart

You need macOS or Linux, Python 3.11 or newer, and [`uv`](https://docs.astral.sh/uv/). From a terminal:

1. Clone the repository if you do not already have a checkout, then enter it:

   ```sh
   git clone https://github.com/Loligplayer33/goodnotes-cli.git
   cd goodnotes-cli
   ```

2. Install the project environment:

   ```sh
   uv sync
   ```

3. Create or refresh the local read view. On macOS, the CLI finds the Goodnotes library automatically:

   ```sh
   uv run goodnotes sync
   ```

   On Linux, give `sync` the path to an available library copy:

   ```sh
   uv run goodnotes sync --library /path/to/Library
   ```

4. List notebooks and read a page. Replace the example name with a notebook name from your library:

   ```sh
   uv run goodnotes --text ls
   uv run goodnotes --text read "Notebook name" --page 1
   ```

## Working with results

Commands return compact JSON by default. Put `--text` before the command to indent the JSON. Public page numbers start at 1. Search stays in the stored index; use `scan-pdfs` as a separate PDF-background check when needed.

Common tasks:

```sh
uv run goodnotes --text search "lecture topic" --notebook "Notebook name"
uv run goodnotes --text scan-pdfs "lecture topic" --notebook "Notebook name"
uv run goodnotes --fields number,recognition pages "Notebook name" --with-recognition
uv run goodnotes read "Notebook name" --page 3 --source handwriting
uv run goodnotes export "Notebook name" --pages 1-3,7 --output /tmp/selected-pages.pdf
```

The search commands inspect different sources. Recognition metadata helps find pages with stored text or pending indexing; `read` returns the stored candidate text. Selected PDF pages keep the listed order.

For task workflows, see [Usage guide](docs/usage.md). For command options and limits, see [Command reference](docs/commands.md). Agents can use `uv run goodnotes schema` for the concise command contract; [Agent guide](docs/agent-guide.md) covers agent workflows and output handling.

## Documentation

- [Usage guide](docs/usage.md) covers notebook tasks; [Command reference](docs/commands.md) lists every option and output.
- [PDFs and handwriting](docs/pdf-and-handwriting.md) explains text and image limits; [Agent guide](docs/agent-guide.md) covers source and output interpretation.
- [Architecture](docs/architecture.md) explains the local read view; [Robustness](docs/robustness.md) records failure behavior and safeguards.
- [Contributing](CONTRIBUTING.md) lists setup and checks; [Releases](docs/releases.md) describes the tagged release process.
