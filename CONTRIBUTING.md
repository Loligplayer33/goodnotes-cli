# Contributing

Contributions should preserve the CLI's read-only access to Goodnotes source files. `sync` reads source files to create or refresh a local copy. Other commands read the validated copy; commands that create PDFs, images, bundles, snapshots, or annotated-PDF registrations write to new CLI-owned paths. Do not include real notebook names, IDs, library paths, email addresses, or extracted note text in code, tests, logs, issues, or documentation.

Agents should follow [AGENTS.md](AGENTS.md) for project-specific safety and review instructions.

## Set up

Install `uv`, then run these commands from the repository root. They create and use the project `.venv`:

```sh
uv sync
uv run goodnotes --help
uv run goodnotes schema
```

Use `uv run` for project commands. Do not install project dependencies globally. The package requires Python 3.11 or newer. Typer handles the CLI, PyMuPDF handles PDF work, and the standard library handles SQLite.

## Make a change

Use generated fixtures for tests and examples. Never run a test or routine development check against a user's Goodnotes Library. If a manual check needs a pre-created private copy, keep it outside the repository and pass it with the global `--sandbox` option before the command. Do not commit the copy or its extracted contents.

Keep the implementation within its package boundary:

| Area | Responsibility |
| --- | --- |
| `library` | Read SQLite rows and map folders, notebooks, pages, text, and search results. |
| `text_index` | Query stored-text counts and mapping diagnostics using connections owned by `library`. |
| `documents` | Resolve copied PDF pages, render pages, and export PDFs. |
| `cache` and `safety` | Validate snapshots, cache transitions, locks, and safe paths. |
| `snapshot` | Copy, verify, and mark a Library snapshot. |
| `cli` and `output` | Parse commands and format structured output and errors. |

Preserve text-source labels and one-based public page numbers. Keep operations on source SQLite databases out of the code; commands should read the validated copy. Before changing a command, inspect `uv run goodnotes COMMAND --help` and `uv run goodnotes schema [COMMAND]`. Update the [command reference](docs/commands.md) when the public interface changes, and update the [README](README.md) for changes to the quickstart or normal user workflow. Keep user-facing operating steps in the [agent guide](docs/agent-guide.md).

See [Architecture](docs/architecture.md) for package responsibilities and the cache/read lifecycle. See [Robustness](docs/robustness.md) for safety behavior and the scenario-to-test map.

## Run checks

Run the checks relevant to the change:

```sh
uv run pytest
uv run ruff check .
uv run mypy src/goodnotes_cli
```

Tests must use generated fixtures. Before submitting, review the diff and `git status` for private data, generated output, and machine-specific paths.

## Submit a change

Describe the behavior that changed and list the checks you ran. Full reconstruction of native Goodnotes handwriting remains unsupported; describe a capability only when the code provides it.
