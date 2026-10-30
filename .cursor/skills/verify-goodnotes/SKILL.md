---
name: verify-goodnotes
description: Run Goodnotes development verification or interpret its receipt for generated journeys, rendering references, installed targets, and copied snapshots.
---

# Verify Goodnotes

Use this skill when checking a Goodnotes CLI change or interpreting a verification
receipt. Read [development verification](../../../docs/verification.md) for the
current runner contract and optional target syntax.

## Run

1. From the repository root, run the required generated checks:

   ```sh
   uv run --locked python scripts/verify.py
   ```

2. Accept the run only when the receipt has `status: pass`, `exit_code: 0`,
   `target.status: pass`, `checks.tree_integrity.status: pass`,
   `checks.target_integrity.status: pass`, and
   `checks.generated_acceptance.status: pass`. Both required suites must report `pass`
   with zero skips. If a private corpus was requested, require its status to
   pass as well. A required skip is `blocked`.
3. Run an installed target with `--cli` only when the installed executable is in
   scope. Run a private corpus with `--private-corpus` only when that check is in
   scope and an outside-repository manifest names an existing validated copy
   and independent Goodnotes PDF references. The checker renders from the copy;
   it does not capture, sync, or control the live library or Goodnotes app.
4. If requested, use `--report` to select a receipt path. Keep the full pytest,
   Ruff, and `ty` checks separate. See [Contributing](../../../CONTRIBUTING.md).

When inspecting a receipt, report `pass`, `fail`, `blocked`, or `unverified` for
each selected check. An unrequested private-corpus check remains `unverified`.
The receipt records source provenance and aggregate check results, not raw
subprocess output. Use the linked suites in
[what the evidence establishes](../../../docs/verification.md#what-the-evidence-establishes)
to choose focused coverage.

## Compare rendering

Expected generated pages must be drawn independently of production decoding and
rendering. The comparison checks dimensions, the whole page, and declared
regions without alignment or resizing. Give every required annotation its own
region, especially each small mark. A broad highlight or stroke must not hide a
missing handwritten mark. Require a known-bad control or deliberate omission for
every defect class, and make the same acceptance rule fail it.

Private corpus checks render one clean background for the selected page in each
case. Regional foreground retention uses that background to separate annotations
from printed page content; whole-page metrics still compare every pixel. The direct
`compare_reference` helper accepts optional `background` and `background_page`
arguments. Manifest version 1 has no background field.

Geometry agreement establishes agreement with the declared pixels. It does not
establish Goodnotes file-format compatibility or full native fidelity. Preserve
one-based page numbers, stable page IDs, source labels, `annotation_status`,
image source, coverage, object counts, warnings, and unknown values.

One bounded two-turn Luna fresh agent pilot on generated fixtures passed a full
annotated PDF export, mark edit, page insertion, and notebook rename and move. It
followed the same stable page ID from position 2 to 3, retrieved only its
selected bundle, and did not export again. This does not measure general agent
study-answer accuracy. The default receipt does not score that pilot; general
accuracy and actual iPad/TCC behavior remain `unverified`.

Keep manifests, copied snapshots, and reference PDFs outside the repository.
Do not copy private names, IDs, paths, note text, command responses, or image
diffs into tracked files, logs, or CI artifacts. The project skill uses standard
`name` and `description` frontmatter; no pstack installation is part of this
workflow.

## Entry points

- [`scripts/verify.py`](../../../scripts/verify.py) runs required suites and
  writes the receipt.
- [`scripts/verification/reference.py`](../../../scripts/verification/reference.py)
  compares pages and runs explicit copied-corpus checks.
- [`tests/support/native_store_fixture.py`](../../../tests/support/native_store_fixture.py) provides shared
  generated native fixtures.
- [`tests/journeys/test_study_journeys.py`](../../../tests/journeys/test_study_journeys.py)
  exercises generated study journeys through the selected console command.
