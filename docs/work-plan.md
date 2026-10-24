# Work plan

This file tracks the first implementation. Tests use generated fixtures and a private library copy outside the repository.

- [x] Read the supplied conversation and inspect the local storage layout.
- [x] Review established CLI conventions and choose the language.
- [x] Copy the Goodnotes databases and attachments without opening a live database.
- [x] Verify the source database hashes after the copy.
- [x] Build snapshot validation and a safe refresh command.
- [x] Build folder lookup, notebook lookup, page ordering, and text search.
- [x] Read handwriting recognition candidates and typed text with source labels.
- [x] Investigate annotated rendering and document its exact limits.
- [x] Build PDF export, page rendering, and an agent reading workflow.
- [x] Implement command help, JSON output, and error handling.
- [x] Run unit, integration, safety, and private snapshot smoke tests.
- [x] Complete storage and command code-review checkpoints and fix the findings.
- [x] Finish the architecture discussion before cache efficiency changes.
- [x] Add tested refresh serialization, safe retention, and unchanged-source reuse.
- [x] Verify the final documentation, packaging, and public file list.
- [x] Create the public GitHub repository.
- [x] Push the reviewed implementation and verify the GitHub checks.
- [ ] Verify the version-tag release workflow.

## Decisions

Python 3.11 or newer runs in a project `.venv`. Typer handles command parsing. PyMuPDF handles PDF text, page assembly, and rendering. SQLite access uses Python's standard library.

Normal commands accept only a marked sandbox. The snapshot command can read source files to make a separate copy. No command edits the Goodnotes app or its production databases.

The repository contains code, synthetic tests, and documentation. Private data, exported notes, credentials, and machine-specific configuration stay outside it.

## Verification notes

The initial private copy includes Databases and Attachments. The projection database passes a quick check. The search database uses Goodnotes' custom mozporter tokenizer, so validation and queries must avoid its virtual FTS tables. Read the ordinary content tables instead.

The first integrated test pass completed with 73 tests. Later independent reviews added cache lifecycle, resource limits, and concurrency regressions. Tests include WAL-only commits, writes during capture, active-pointer failure, output conflicts, bounded text, PDF signatures, and isolated command subprocesses. The final cache review passed its targeted regressions. Freshness metadata documents only what the inventory checks observed; it does not promise latest-at-return state.

Final local validation: 102 tests passed. Ruff formatting and lint checks passed. Mypy passed. The source distribution and wheel contain only code, synthetic tests, and documentation. The installed wheel also runs outside the checkout. GitHub CI passed on Linux with Python 3.11, 3.12, and 3.13, and on macOS with Python 3.12. Linux passed 101 tests and skipped the APFS-specific test; macOS passed all 102 tests. The tagged release remains to be verified.
