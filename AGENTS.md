# Repository agent guidance

Keep changes within the existing package boundaries. Use the code, configuration, and command help as the source of truth for flags and module details.

## Safety and privacy

- Use generated fixtures for tests and examples. Manual checks may use only a pre-existing private copy outside the repository. Keep notebook names, IDs, paths, email addresses, and note text out of tracked files, logs, and generated repository output.
- Never run the default `sync` against a live Goodnotes Library during development. A production capture requires an explicit user request and the file-copy-only workflow in [Architecture](docs/architecture.md). Do not control Goodnotes or open a source SQLite database.
- Treat the source Library, source databases, and completed snapshots as immutable. SQLite reads must target a validated copy in read-only mode.
- Use the project virtual environment with `uv run`; never install project dependencies globally.

## Contracts and reviews

- Preserve compact JSON as the default, one-based public page numbers, and source labels and warnings. Keep unknown or unavailable data distinct from absent, false, or zero values.
- For cache, snapshot, lock, or safety changes, read [Architecture](docs/architecture.md) and [Robustness](docs/robustness.md). For agent notebook workflows or output interpretation, read the [Agent guide](docs/agent-guide.md). Use [Contributing](CONTRIBUTING.md) for setup, checks, and documentation updates.
- Get an independent correctness and maintainability review for major code changes. For material architecture or public-interface changes, get an independent review and debate the design when those tools are available. Routine fixes need no special permission.

If `.local/HANDOFF.md` exists, consult it only when a task needs the location or handling of a pre-existing private copy. Keep its machine-specific details out of committed files.
