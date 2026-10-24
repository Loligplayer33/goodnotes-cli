# Changelog

Notable changes to the project are recorded here. Versions follow Semantic Versioning.

## [Unreleased]

### Added

- Notebook-scoped search and PDF scans, source-coverage diagnostics, metadata-only handwriting discovery, and selected-page PDF export.
- Repository agent guidance in `AGENTS.md`, shared with Claude through a `CLAUDE.md` symlink.

### Changed

- Browse and read commands open the initialized cache lock read-only. Sync owns lock creation, so restricted agents need no cache write access to read notes.
- `doctor` includes the selected cache directory, snapshot path, and capture time. Cache setup, permission, and validation errors include a specific path and remedy.
- Reorganized the README around capabilities, setup, and a complete command index, with separate task and command references.
- Added examples and usage limits to built-in command help. `schema COMMAND` now returns registered argument and option metadata without loading notes.
- Updated task guidance for scoped search, explicit PDF-background scans, recognition status, and selected-page export.
- Corrected the schema's folder option syntax and included `snapshot`, help, and version options.

### Fixed

- Field selection on bundle results now handles numeric page lists without crashing.
- Field-selection errors identify the response level and valid keys. Snapshot metadata is selectable on top-level responses and always retained.
- Invalid field projections fail before files or cache state are written, including when row results are empty.
- Indexed PDF text is checked against the current page's document, attachment, and PDF page before reading, searching, or counting it.
- Partial index schemas report unavailable mappings instead of crashing, and missing metadata is reported as unknown when it cannot be inspected.
- Explicit managed-copy paths containing `..` retain the reader lock during refresh and pruning.
- Whole-library PDF diagnostics use indexed identity lookups instead of repeated template scans.

## [0.1.0] - 2026-10-24

### Added

- A local CLI for syncing a library copy, browsing notebooks and pages, and searching stored text.
- PDF page rendering and export commands, including page bundles with source-labeled text.
- A read-only cache workflow that keeps normal browse commands away from the source library.
