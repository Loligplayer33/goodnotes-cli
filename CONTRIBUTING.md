# Contributing

Contributions should preserve the CLI's read-only boundary. `sync` may read Goodnotes files to create a verified local copy. Other commands read the active copy. Do not include real Goodnotes data in a patch.

## Set up the project

Install `uv`, then run the following from the repository root. `uv sync` creates and uses the project `.venv`.

```sh
uv sync
uv run goodnotes --help
uv run goodnotes schema
```

Use `uv run` for project commands so Python packages stay inside the project environment. Do not install this project's dependencies globally.

## Make a change

Use synthetic data for tests and examples. Keep storage queries in the library or document modules, route active-cache and sandbox validation through `safety`, and keep command parsing and JSON formatting in the CLI. Preserve the source label for extracted text and the 1-based page numbering shown to users. For a development task that needs a pre-created private copy, pass it with the global `--sandbox` option before the command.

Before changing a command, check its current help output and update this repository's docs if the public interface changes. Full reconstruction of native Goodnotes handwriting is still pending, so describe that capability only when the code supports it.

## Run checks

Run the checks relevant to the change:

```sh
uv run pytest
uv run ruff check .
uv run mypy src/goodnotes_cli
```

Tests must use generated fixtures. A sandbox smoke test may use a private copy kept outside the repository. Never commit that copy or its extracted contents.

## Submit a change

Describe the behavior that changed and include the checks you ran. Before submitting, review the diff and `git status` for private data, generated output, and machine-specific paths.
