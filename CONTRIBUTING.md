# Contributing

Use generated fixtures for tests and examples. Never use the live Goodnotes Library in development, and keep real notebook names, IDs, paths, email addresses, and note text out of tracked files, logs, and generated repository output. See [AGENTS.md](AGENTS.md) for repository safety and review instructions.

## Set up

Install `uv`, then run from the repository root:

```sh
uv sync --locked --all-groups
uv run --locked goodnotes --help
uv run --locked goodnotes schema
```

This creates and uses the project `.venv` from `uv.lock`. The project requires Python 3.11 or newer. Run project commands through `uv run`; do not install project dependencies globally.

## Make a change

Keep changes within the existing package boundaries in [Architecture](docs/architecture.md). Before changing a command, inspect `uv run --locked goodnotes COMMAND --help` and `uv run --locked goodnotes schema [COMMAND]`. After changing command signatures or help, regenerate [commands](docs/commands.md) with `uv run --locked python scripts/docs.py`; do not edit that generated page by hand. Update [README](README.md) for quickstart changes, [everyday tasks](docs/usage.md) for user steps, and the [agent guide](docs/agent-guide.md) for agent workflows.

For cache, snapshot, lock, and safety changes, read [Architecture](docs/architecture.md) and [Robustness](docs/robustness.md). For agent notebook workflows or output interpretation, read the [Agent guide](docs/agent-guide.md).

## Run checks

```sh
uv run --locked pytest -n 4
uv run --locked ruff format --check src tests scripts
uv run --locked ruff check src tests scripts
uv run --locked ty check --error-on-warning src/goodnotes_cli tests scripts
```

`pytest -n 4` runs tests in four worker processes. Keep generated fixtures isolated per test. Decoder property tests can be run directly with `uv run --locked pytest tests/test_decoder_properties.py`; Hypothesis stores local failures in ignored `.hypothesis/`.

The generated study-session acceptance runner is:

```sh
uv run --locked python scripts/verify.py
```

It requires every selected check to run with zero skips. The receipt is written to `.local/verification/report.json` by default. This focused runner does not replace the full test, lint, and type-check commands above. For an installed executable or a copied private reference corpus, follow [Development verification](docs/verification.md).

Before submitting, review the diff and `git status` for private data, generated output, and machine-specific paths. Describe the behavior change and the checks you ran. Native ink reconstruction remains experimental; report supported object counts, coverage, and rendering limits. Tests that use RocksDB must create their own store and skip with a reason when the optional runtime is unavailable.

## Documentation

Follow [Documentation maintenance](docs/documentation.md) when changing documentation. Run its link check and strict site build before submitting docs changes.
