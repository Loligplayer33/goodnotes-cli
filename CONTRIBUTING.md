# Contributing

Use generated fixtures. Never develop against a live Goodnotes Library or add real notebook names, IDs, paths, email addresses, or note text to repository output. Follow [AGENTS.md](AGENTS.md) for safety and review rules.

## Setup

Install `uv`, then run from the repository root:

```sh
# Optional: set a writable cache path when UV's default is unavailable.
# export UV_CACHE_DIR=/path/to/writable/uv-cache
uv sync --locked --all-groups
uv run --locked goodnotes --help
uv run --locked goodnotes --version
uv run --locked goodnotes schema
```

`uv sync` prepares the project-local `.venv` by default; `UV_PROJECT_ENVIRONMENT` can select another
path. Use `uv run` for project commands. Python 3.11 or newer is required; do not install
dependencies globally. If UV's default cache is unavailable, export `UV_CACHE_DIR` to a writable
directory before running these commands. It stores packages separately from the project environment
and Goodnotes CLI data.
`goodnotes --version` prints `VERSION (source FIRST12SHA)`. The schema index and targeted schema
include `build.version` and the full `build.source_sha256`; `doctor` includes corresponding CLI and
snapshot provenance. Choose a separate writable Goodnotes data directory with `--data-dir` or
`GOODNOTES_DATA_DIR`. Native rendering also needs a compatible RocksDB runtime and writable scratch;
set `GOODNOTES_SCRATCH_DIR` to an existing writable directory if system temporary storage is
unavailable. Text browsing and background images do not need RocksDB.

`uv run --locked --no-sync` is only for commands in an already prepared environment. It skips
dependency synchronization and therefore does not install or verify the environment.

## Changes and checks

Keep code within the package boundaries in [Architecture](docs/architecture.md). Command help and `goodnotes schema` are the source of truth for flags and output. Update [README](README.md) for setup changes and [usage](docs/usage.md) or the [agent guide](docs/agent-guide.md) for workflow changes.

```sh
uv run --locked pytest -n 4
uv run --locked ruff format --check src tests scripts
uv run --locked ruff check src tests scripts
uv run --locked ty check --error-on-warning src/goodnotes_cli tests scripts
uv run --locked python scripts/verify.py
```

The focused verification runner does not replace the full test, lint, and type checks. See [Development verification](docs/verification.md) for installed-CLI or private-copy comparisons. RocksDB tests must create generated stores and skip with a reason if the optional runtime is unavailable.

## Documentation

Build the strict docs site or serve a local preview:

```sh
uv run --locked --group docs mkdocs build --strict
uv run --locked --group docs mkdocs serve
```

Keep user tasks in [usage](docs/usage.md), agent workflows in the [agent guide](docs/agent-guide.md), and flags in command help/schema. Review `git diff` and `git status` for private data and generated files before submitting.
