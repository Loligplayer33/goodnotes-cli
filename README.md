# Goodnotes CLI

Browse, search, read, and export a local Goodnotes library. The CLI reads a validated
copy and returns JSON for scripts and agents. It does not edit notebooks or control Goodnotes.

## Install

You need Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.11 or newer.
Live library discovery works on macOS; Linux needs an existing Library folder copy.

```sh
git clone https://github.com/Loligplayer33/goodnotes-cli.git
cd goodnotes-cli
uv sync --locked
uv run --locked goodnotes --help
```

Help and `goodnotes schema` work without loading notes. Run examples from the checkout.

## Read your first page

For your Goodnotes library on this Mac:

```sh
uv run --locked goodnotes sync
uv run --locked goodnotes --text ls
uv run --locked goodnotes --text read "Course Notes" --page 1
```

Replace `"Course Notes"` with a returned notebook name, full path, or ID.
Use `sync --library /path/to/Library` for an explicit source containing `Databases/`
and `Attachments/`. Sync needs source read and cache write access; later reads use
the copy. On macOS, respond to any system privacy prompt for your terminal or agent.
Development checks use generated fixtures or a pre-existing private copy, never the default live sync.

Page numbers start at 1. `--text` indents JSON; global options go before the command.
`read` labels PDF text, stored handwriting candidates, and typed text separately.
The CLI does not run OCR on images.

## Choose a task

| Goal | Guide |
| --- | --- |
| Find, search, read, or export notes | [Everyday tasks](docs/usage.md) |
| View handwriting or use a Goodnotes PDF export | [Images and handwriting](docs/pdf-and-handwriting.md) |
| Give an agent page evidence | [Agent guide](docs/agent-guide.md) |
| Look up commands, output, or pagination | [Command reference](docs/commands.md) |
| Contribute or preview the searchable docs | [Contributing](CONTRIBUTING.md) |
| Understand storage and safety | [Architecture](docs/architecture.md) and [failure behavior](docs/robustness.md) |

Native reconstruction is approximate and needs an optional RocksDB runtime.
Standalone render/export default to backgrounds without native ink; bundle defaults
to native images and PDF text. See [layer choices](docs/pdf-and-handwriting.md#choose-a-layer).

Report problems in [GitHub Issues](https://github.com/Loligplayer33/goodnotes-cli/issues)
with a synthetic example and redacted private data. [MIT license](LICENSE).
