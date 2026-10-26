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
uv run --locked goodnotes --version
uv run --locked goodnotes schema
```

`uv sync` prepares the project-local `.venv`. Help, version, and schema work without loading notes.
Version prints the package release and the first 12 characters of its source fingerprint. Schema
includes the full build identity. Run examples from the checkout.

If UV cannot write its default package cache in a restricted shell, export `UV_CACHE_DIR` to a
writable path before running the setup commands. This is separate from the project environment and
Goodnotes CLI snapshot storage; use `--data-dir` or `GOODNOTES_DATA_DIR` to select a writable CLI
data directory.

## Read your first page

For your Goodnotes library on this Mac:

```sh
uv run --locked goodnotes sync
uv run --locked goodnotes --text ls
uv run --locked goodnotes --text read "Course Notes" --pages 1
```

Replace `"Course Notes"` with a returned notebook name, full path, or ID.
Use `sync --library /path/to/Library` for an explicit source containing `Databases/`
and `Attachments/`. Sync needs source read and cache write access; later reads use
the copy. On macOS, respond to any system privacy prompt for your terminal or agent.
Development checks use generated fixtures or a pre-existing private copy, never the default live sync.

Page numbers start at 1. `--text` indents JSON; global options go before the command.
`read`, `search`, and `bundle` default to indexed PDF text and typed textboxes. Add
`--source handwriting` or `--source all` to include unverified stored handwriting recognition
candidates. Use `read --pages` to select pages; `--page` remains a hidden compatibility option.
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

Render, export, and bundle default to native reconstruction, which needs a compatible RocksDB
runtime and writable scratch storage; coverage is approximate and full visual fidelity is
unverified. Pass `--layer background` to request the background without reconstructed ink. See
[layer choices](docs/pdf-and-handwriting.md#choose-a-layer).

Report problems in [GitHub Issues](https://github.com/Loligplayer33/goodnotes-cli/issues)
with a synthetic example and redacted private data. [MIT license](LICENSE).
