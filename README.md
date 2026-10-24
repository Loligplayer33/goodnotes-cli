# Goodnotes CLI

Goodnotes CLI lets you browse, search, read, and export a local Goodnotes library from the command line. It reads a managed copy of the library and does not edit Goodnotes or its source files.

Live library discovery is for macOS. On other systems, point `sync --library` at an available copy of a Goodnotes `Library` folder that contains `Databases/` and `Attachments/`.

## What it can do

| Task | Support | Limit |
| --- | --- | --- |
| Browse notebooks and pages | Yes | Reads the last synced copy. Run `sync` again after Goodnotes changes the library. |
| Search and read text | Yes | `read` returns PDF text, stored handwriting recognition candidates, and typed text. `search` checks indexed text from these sources when available. The CLI does not OCR images or render native ink. |
| View and export page backgrounds | Yes | Uses each page's PDF or image background. Native Goodnotes ink is not included. |
| View and export annotations | With a supplied PDF | You must export and register a PDF from Goodnotes. The CLI cannot verify that it includes annotations or that its page order matches. |
| Edit Goodnotes notebooks | No | The CLI does not write to the Goodnotes library. |

## On this page

- [Commands](#commands)
- [Quickstart](#quickstart)
- [Working with results](#working-with-results)
- [Documentation](#documentation)

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

You need Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/). From a terminal:

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

   On another system, give `sync` the path to an available library copy:

   ```sh
   uv run goodnotes sync --library /path/to/Library
   ```

4. List notebooks and read a page. Replace the example name with a notebook name from your library:

   ```sh
   uv run goodnotes --text ls
   uv run goodnotes --text read "Notebook name" --page 1
   ```

## Working with results

Commands return compact JSON by default. Put `--text` before the command to indent the JSON for inspection. `--text` changes the formatting, not the kind of output. Public page numbers start at 1.

For task workflows, see [Usage guide](docs/usage.md). For command options and limits, see [Command reference](docs/commands.md). Agents can use `uv run goodnotes schema` for the concise command contract; [Agent guide](docs/agent-guide.md) covers agent workflows and output handling.

## Documentation

- [Usage guide](docs/usage.md) walks through sync, browsing, reading, search, and export tasks.
- [Command reference](docs/commands.md) lists every command's arguments, options, and output notes.
- [PDFs and handwriting](docs/pdf-and-handwriting.md) explains page backgrounds, native ink, and registered PDF exports.
- [Agent guide](docs/agent-guide.md) covers agent workflows and output handling.
- [Architecture](docs/architecture.md) describes how the local read view is built.
- [Robustness](docs/robustness.md) records concurrent updates, incomplete data, and output safeguards.
- [Contributing](CONTRIBUTING.md) lists development setup and local checks.
- [Releases](docs/releases.md) describes the tagged release process.
