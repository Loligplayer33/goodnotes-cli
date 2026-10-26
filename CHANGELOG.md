# Changelog

Notable changes to the project are recorded here. Versions follow Semantic Versioning.

## [Unreleased]

### 0.2.0 interface and rendering updates

- Native rendering and full-notebook PDF export now include pages whose copied store query returns no records. Their status and warnings preserve uncertainty about annotation absence; unavailable stores and failed reads remain errors.
- The main help focuses on ten everyday commands. Specialist commands, including `attach-pdf`, remain available through targeted help and schema. The schema command index now comes from registered commands.
- Read, search, and bundle default to PDF and typed text. Stored handwriting candidates require `--source handwriting` or `--source all`. Pages include recognition metadata without an extra switch.
- Render and export default to native output. Use `--layer background` explicitly to omit reconstructed ink. Existing `--page`, `--with-recognition`, and `--json` aliases remain accepted but are hidden from normal help.
- Folder listings can filter item kind before pagination. Notebook-scoped diagnostics separate local page-index coverage from library-wide catalog issues, without treating index gaps as lost notes.
- Version and schema identify installed source content. New snapshot markers record the CLI producer, copied SQLite format values, and installed-app metadata when known. Historical provenance remains unknown.

### Added

- Native rendering of observed filled paths, outlined ellipses, variable-width pencil strokes, and copied PNG/JPEG image attachments, with separate path and image counts.
- Temporary render and bundle storage with image reuse, shared PNG payloads, a 100 MB cap, one-day idle expiry, and protected returned paths. Explicit `--output` destinations remain persistent.
- Read-only artifact-directory access hints in `doctor --access-only`.
- Notebook-scoped search and PDF scans, source-coverage diagnostics, metadata-only handwriting discovery, and selected-page PDF export.
- Repository agent guidance in `AGENTS.md`, shared with Claude through a `CLAUDE.md` symlink.
- A project-local verification skill and development runner for generated study
  journeys, independent render references, and optional installed-wheel or
  copied-snapshot checks.

### Changed

- Type checking uses ty as the sole required checker, including warnings; mypy is no longer a development dependency.
- Page hydration, search bindings and PDF scans reuse work within one read view. Multipage rendering and export share native-store and PDF resources.
- Non-PDF commands defer PDF-library loading. Routine CLI contract tests run in process while process-specific tests retain subprocess coverage.
- Incremental capture reuses unchanged original SQLite digests with copied-byte verification and reuses compatible prior projection validation for unchanged complete families.
- Browse and read commands open the initialized cache lock read-only. Sync owns lock creation, so restricted agents need no cache write access to read notes.
- `doctor` includes the selected cache directory, snapshot path, and capture time. Cache setup, permission, and validation errors include a specific path and remedy.
- Reorganized the README around capabilities, setup, and a complete command index, with separate task and command references.
- Added examples and usage limits to built-in command help. `schema COMMAND` now returns registered argument and option metadata without loading notes.
- Updated task guidance for scoped search, explicit PDF-background scans, recognition status, and selected-page export.
- Corrected the schema's folder option syntax and included `snapshot`, help, and version options.

### Fixed

- Separate catalog root namespaces before navigation and search. A cache-local root selection keeps unrelated libraries out of normal browsing, preserves exact selectors, and avoids cross-library path collisions without folder-name rules.

- Missing native handwriting and drawing forms now decode with bounded geometry validation. Renderer upgrades and changed copied image attachments invalidate cached page renders.
- Added the observed two-point open-line shape with bounded line translation, exact direct image-resource binding, and an explicit count and warning for image records that omit body field 6. Generated installed-CLI journeys now compare supported schema combinations against independent per-object references and reject stale renderer-cache entries.
- Multi-page native bundles can now retain successful page images and all selected pages' text when a page-local native render fails; failed rows carry structured errors and no image, while single-page and all-page failures remain errors.
- Filesystem overlap checks recognize case and directory-identity aliases. Cleanup does not follow symlinks or change their targets' permissions.
- Source edits during the final freshness scan preserve a validated view and trigger another refresh; watch coalesces transient inventory changes.
- Genuine hot rollback journals recover only on independent staging copies. Fallback SQLite output leaves WAL mode before publication.
- Search snippets retain matches after Unicode casefold expansion. Missing mapped PDFs and malformed catalog values report specific diagnostics.
- Separate native start-only dots no longer gain connecting lines. Ambiguous mixed-start geometry is reported as unsupported while single-start stroke ordering is preserved.
- Query-time cache errors retain structured details, doctor projections validate before access, and closed output pipes avoid generic operation errors.
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
