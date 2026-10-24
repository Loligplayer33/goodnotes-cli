# Changelog

Notable changes to the project are recorded here. Versions follow Semantic Versioning.

## [Unreleased]

### Changed

- Reorganized the README around capabilities, setup, and a complete command index, with separate task and command references.
- Added examples and usage limits to built-in command help. `schema COMMAND` now returns registered argument and option metadata without loading notes.
- Corrected the schema's folder option syntax and included `snapshot`, help, and version options.

### Fixed

- Field selection on bundle results now handles numeric page lists without crashing.

## [0.1.0] - 2026-10-24

### Added

- A local CLI for syncing a library copy, browsing notebooks and pages, and searching stored text.
- PDF page rendering and export commands, including page bundles with source-labeled text.
- A read-only cache workflow that keeps normal browse commands away from the source library.
