# Agent guide

This repository builds a local CLI for reading a Goodnotes library through a verified copy. Use generated fixtures for development and tests. Do not add real notebook names, identifiers, library paths, email addresses, or extracted note text to code, tests, logs, issues, or docs.

## Set up and inspect

Run commands through `uv` so project dependencies stay in `.venv`:

```sh
uv sync
uv run goodnotes --help
uv run goodnotes schema
uv run pytest
```

The normal user workflow starts with `uv run goodnotes sync`, then browse commands read the active cache. Agents should not run sync against a user's Goodnotes Library during routine tests. `schema` describes the command contract without loading notebook data. Use `--sandbox PATH` only for an explicit development read view. A managed cache snapshot passed with `--sandbox` can be removed after a later sync; create a named snapshot outside the managed cache if a task needs a persistent copy.

## Preserve the read boundary

`sync` is the only normal command that reads source Library files. It never opens a source SQLite database, controls the Goodnotes app, or writes to its library. Other commands read the validated active copy. Tests should call `sync` only with generated fixtures. If a task explicitly requires a private copy, keep it outside the repository and do not copy its contents into examples or test output.

The cache has two persistent locks. `.sync.lock` serializes refreshes. `.view.lock` protects active pointer resolution and each command's complete read, then lets sync publish and prune after readers finish. Keep the shared view lock through the final database or attachment read. Do not move a Goodnotes-side lock or SQLite connection into this design. Commands do not refresh in the background.

When source and active-cache stat inventories match, sync reuses that generation without hashing all file contents. Changed cache metadata triggers recapture. Use `sync --force` to bypass reuse and create a fully verified generation even when the inventories match. Changed files are cloned with copy-on-write where supported or copied independently, then validated.

If a source change overlaps a capture attempt, sync retries. If no attempt succeeds, it keeps the prior active view. After capture, sync compares the source inventory from before capture with a final inventory. This check includes any earlier capture attempts and runs before sync waits for `.view.lock` to publish. If the inventories differ, sync publishes the validated view and returns `source_changed_during_refresh: true`, `captured_at`, and a warning. If they match, the flag is false; this does not mean the source stayed unchanged through command return. Updates after the final check, including while waiting to publish, are detected by the next sync. Sync omits the reusable source fingerprint after a mismatch, so the next sync captures a fresh view. `captured_at` is the validated view's capture time. Staging left by a process kill is removed on the next sync; Ctrl-C cleans the partial capture.

## Work with results

The CLI emits compact JSON by default. `--text` indents that JSON; `--json` selects compact JSON. Global options such as `--data-dir`, `--sandbox`, `--fields`, `--text`, `--json`, and `--expect-snapshot` go before the command. `--fields` selects columns from `ls`, `pages`, and `search` rows.

Page numbers are 1-based. `ls`, `tree`, `pages`, and `search` have bounded results with offset pagination. `scan-pdfs` has a page offset cursor. Each paged result includes its snapshot generation ID in `snapshot`. Pass that value to the global `--expect-snapshot ID` option on later pages. If sync changes the active view, the CLI returns a `stale_snapshot` error; restart pagination against the new snapshot rather than combining generations.

`read` defaults to page 1, accepts at most 20 distinct pages, and caps combined text at 12,000 characters by default. `bundle` accepts at most 10 pages and uses one character budget across their text. Both report truncation. `search` returns short snippets and identifies its coverage as `stored-index`. Use `scan-pdfs` to search copied PDF backgrounds that the index missed. It does not search handwriting or the stored index. `--page-limit` defaults to 200 and cannot exceed 250; continue with `next_page_offset`.

## Keep text sources clear

The CLI labels text as PDF, handwriting, or textbox data. `read` uses indexed PDF text when available and can extract text from a mapped PDF background in the copy when the index misses it. Neither route decodes native annotations. Text flattened into an imported PDF may still be extracted. Image-only writing produces no PDF text. Handwriting values are stored recognition candidates, not a visual transcription of ink.

The `background` layer assembles available PDF pages in current notebook order and does not overlay native Goodnotes ink. The `annotated` layer requires a user-exported PDF registered with `attach-pdf`. The checks confirm page count and file integrity but cannot verify annotation inclusion or page order. Annotated results report `includes_handwriting: null` and `annotation_status: user-provided-unverified`. Keep the registered PDF available and unchanged.

## Extend the package

Keep SQLite queries in `library`, PDF handling in `documents`, cache checks and locks in `cache` and `safety`, and command parsing and output formatting in `cli` and `output`. Preserve source labels and 1-based public page numbers. Add generated fixtures for behavior that depends on ordering, source labels, cache transitions, or safety boundaries. Check command help and update the public docs when the interface changes.

Run focused tests and required checks before reporting a change. Check `git status` and the diff for private data, generated output, and machine-specific paths. See [Robustness and failure behavior](robustness.md) for scenario-to-test coverage and [Contributing](../CONTRIBUTING.md) for local checks.
