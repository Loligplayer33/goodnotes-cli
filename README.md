# Goodnotes CLI

Browse, search, read, and export a local Goodnotes library from the command line.
Goodnotes CLI reads a validated copy and returns JSON for scripts and agents.
It does not edit your notebooks or control the Goodnotes app.

## Start here

You need Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.11 or newer.
Live library discovery works on macOS. Linux users need an existing copy of a Goodnotes `Library` folder.

```sh
git clone https://github.com/Loligplayer33/goodnotes-cli.git
cd goodnotes-cli
uv sync --locked
uv run --locked goodnotes --help
uv run --locked goodnotes schema
```

These checks do not load your notes. Next, [connect a library and read a page](docs/getting-started.md).
Browsing, text reads, PDF searches, and background exports need no native rendering runtime.

## What do you want to do?

| Task | Guide |
| --- | --- |
| Find a notebook or read a lecture page | [Everyday tasks](docs/usage.md) |
| Give an agent page images and text | [Inspect a page](docs/usage.md#inspect-a-page-with-an-agent) |
| Find text the stored index missed | [Search notes and PDF backgrounds](docs/usage.md#search-notes-and-pdf-backgrounds) |
| Include handwriting in an image or PDF | [Choose an image layer](docs/pdf-and-handwriting.md) |
| Give an agent access only to the copy | [Access and refresh](docs/access.md) |
| Look up flags and defaults | [Command reference](docs/commands.md) |
| Fix a setup or rendering error | [Troubleshooting](docs/troubleshooting.md) |
| Work on the CLI | [Contributing](CONTRIBUTING.md) |

## Know what you are reading

| Output | What it contains |
| --- | --- |
| `read` / `search` | Source-labelled text. Stored handwriting candidates are not a verified transcription. |
| `render` / `export` | Page backgrounds by default. Add `--layer native` for supported reconstructed ink. |
| `bundle` | Native page images and PDF text by default. Native rendering needs an optional RocksDB runtime and remains approximate. |
| `--layer annotated` | A PDF you exported from Goodnotes and registered with `attach-pdf`. |

The CLI does not run OCR on images. A refresh sees files already on the Mac;
it cannot confirm whether an iPad edit has arrived.

See the [documentation index](docs/index.md) for user and technical guides, or
[preview the searchable site locally](docs/documentation.md#preview-and-check).
Report problems in [GitHub Issues](https://github.com/Loligplayer33/goodnotes-cli/issues), using synthetic examples and redacting private note data.
The project uses the [MIT license](LICENSE).
