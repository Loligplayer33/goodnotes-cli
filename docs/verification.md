# Development verification

The verification runner checks generated study-session workflows and visual
references through the CLI. It is a development tool; it adds no production CLI
options or runtime dependencies. The runner writes a compact JSON receipt with
`pass`, `fail`, `blocked`, and `unverified` statuses so a skipped required check
cannot be mistaken for a pass.

## Run the required checks

From the repository root, run:

```sh
uv run python scripts/verify.py
```

The runner executes the required generated acceptance suites in
[`tests/test_study_journeys.py`](../tests/test_study_journeys.py) and
[`tests/test_verification_reference.py`](../tests/test_verification_reference.py).
They use generated fixtures; the default CI path does not read private notebooks.
Each required test must execute and pass with zero skips for the acceptance check
to pass. A required skip is `blocked`, including when the native engine is
unavailable.
The default receipt path is `.local/verification/report.json`. Use `--report` to
choose another destination:

```sh
uv run python scripts/verify.py --report /caller/verification-report.json
```

If the destination already exists, the runner replaces it only when it is a
receipt with the `goodnotes-verification/v1` schema. It leaves unrelated files
untouched and reports a write failure.

Run with a compatible RocksDB runtime before treating the required checks as
passed.

- `pass`: the selected check ran and met its acceptance rule.
- `fail`: a check found a mismatch, the source tree changed, or receipt writing failed.
- `blocked`: a requested check could not run to completion.
- `unverified`: optional evidence was not requested or was not exercised.

The receipt records the source commit, whether the checkout is dirty, and a
content fingerprint for tracked and eligible untracked source files. It checks
that the tree and Git status stay stable during the run, and identifies the
target, environment, selected checks, and checks actually executed. Read both
the commit and fingerprint when reporting what code passed.

This focused runner does not replace the full `uv run pytest`, Ruff, and `ty`
checks in [Contributing](../CONTRIBUTING.md). CI runs the required generated
journeys in the macOS job and also tests the explicitly installed wheel there.

## Check an installed distribution

Build and install the wheel into an isolated environment using the steps in
[`ci.yml`](../.github/workflows/ci.yml). Then pass the absolute path to its
`goodnotes` executable:

```sh
uv run python scripts/verify.py \
  --cli /absolute/path/to/isolated-environment/bin/goodnotes
```

The runner invokes that executable from outside the checkout and removes
`PYTHONPATH`. It resolves the console script's Python interpreter, probes the
package origin outside the checkout, and records the version, external-origin
result, and installed/source package fingerprints. It blocks a target whose
package origin is inside the checkout or whose installed package content does
not match the current source package. The receipt does not store local executable
or import paths. The macOS CI job builds the wheel, installs it outside the
checkout, and runs the required generated study journeys through that
executable, including the schema matrix and failure controls. Its small
synthetic PDFs exercise the CLI's PDF handling; private lecture exports and
snapshots stay outside public CI. The separate package job's `goodnotes schema`
smoke check does not replace these journeys.

## Use a copied private reference corpus

An explicit `--private-corpus` run compares output from an already existing,
validated copy with independently exported Goodnotes PDF pages:

```sh
uv run python scripts/verify.py \
  --private-corpus /outside/repository/manifest.json
```

Keep the manifest, copied snapshot, and reference PDFs outside the repository.
The runner accepts relative paths from the manifest directory. Each manifest has
exactly the top-level members `version` (set to `1`) and `cases`. A case supplies
these fields:

| Field | Meaning |
| --- | --- |
| `sandbox` | Path to a pre-existing marked snapshot copy. |
| `notebook` | Exact selector for a notebook in that copy. |
| `page` | One-based public page number in the copy. |
| `reference` | Path to a PDF exported independently from Goodnotes. |
| `reference_page` | One-based page number in that PDF. |
| `reference_provenance` | Must be `Goodnotes-export`. This is a declaration, not an authenticated producer check. |
| `regions` | One to 32 normalized `[x0, y0, x1, y1]` boxes with finite 0-to-1 `max_mean_error`, `max_changed_fraction`, and `min_foreground_retention` limits. |
| `pixel_tolerance` | Maximum per-channel RGB difference as a fraction from 0 to 1. |
| `dpi` | Optional raster resolution from 72 to 300; defaults to 144. |

Version 1 accepts only the listed case fields and optional `dpi`. Each region
contains exactly `box`, `max_mean_error`, `max_changed_fraction`, and
`min_foreground_retention`; box coordinates must increase within the normalized
0-to-1 page range, and each limit must be a finite fraction from 0 to 1. The
whole-page comparison uses the strictest regional limits. Each region must
contain foreground under the comparator's selected background rule.

The private checker renders one clean background image for the selected page in
each case and uses it to compare native render and bundle images. This masks
printed page content only when measuring regional foreground retention. The whole-page
comparison still measures every pixel, including the background. Manifest v1
does not need a background field. The development helper `compare_reference`
also accepts optional `background` and `background_page` arguments for direct
comparisons. Without a background argument, it uses the white-background rule
for regional foreground retention.

Give every required annotation its own region, especially each small handwritten
mark. Do not combine a small mark with a broad stroke, highlight, or other large
object. A large object can dominate broad-region pixel metrics and hide a missing
small mark. Keep the whole-page check and require each annotation's own region to
fail when that mark is missing.

For each case, the runner checks the notebook, renders the selected native page,
exports a bundle for that page, and compares both images with the declared PDF
page. It uses the pre-existing copy through the explicit sandbox option. It does
not capture or sync a live library, register a PDF, open a source database with
SQLite or RocksDB, or control Goodnotes. Temporary CLI data, scratch, and images
are removed after the check. A change to the manifest, reference PDF, or
snapshot-marker bytes during the comparison invalidates the case.

The receipt contains aggregate private-case outcomes, a manifest digest, and an
opaque fingerprint of the manifest, reference PDFs, and snapshot-marker bytes. It
does not contain notebook selectors, private paths, note text, CLI output, or
image diffs. Changes to those fingerprinted inputs during the run invalidate the
case. Do not copy private manifest contents into tracked files, CI artifacts, or
logs. When no private corpus is requested, that optional check remains
`unverified`.

## What the checks establish

The generated study journeys cover a whole-notebook native annotated PDF export,
discussion of a selected page, a later generated-source edit and sync, and
retrieval of only selected current pages. The PDF uses generated source records
and tests the CLI renderer. It is not a Goodnotes-app export. The journeys also
cover stable page identities across insertion,
reordering, and notebook rename or move; explicit missing and deleted identities;
stale snapshot errors; expired image recovery; and errors when native or
annotated evidence is unavailable. Numeric page positions are not durable
identities.

`test_study_supported_native_schema_matrix_matches_independent_references`
also runs the installed CLI against generated pen and highlighter records,
identity variants, filled paths, an outlined ellipse, two-point open lines with
and without a bounded translation, variable-width brush geometry, and exact
image attachment layouts. Image cases cover explicit attachment binding, direct
body-resource lookup, descriptor-resource lookup, a page-edge clipped image, and
body field 6 present or absent. The omitted-field case keeps an explicit count
and warning; its effect on application appearance or visibility remains
unverified. Every candidate
object has an independently drawn reference region, and a reference with that
object omitted must fail the same comparison. Managed cache hits and persistent
bundle manifests retain native counts and warnings. A deliberately seeded
renderer-version cache entry contains a background-only image, so the current
renderer must regenerate it before the installed CLI can pass.

Mixed-page and unsupported-only journeys keep genuinely unknown geometry and
object types fail-closed. A multi-page request with a recoverable page-local
native error keeps successful images and source-labelled text, preserves the
requested order, marks the failed row with `image: null` and `render_error`, and
reports `status: "partial"` plus `failed_pages`. The generated journey covers a
failed page first and in the middle, managed and persistent outputs, healthy
requests after failure, and the single-page all-failed path, which still returns
a structured error without publishing a bundle. The CLI does not replace a
failed image with the page background.

Generated reference pages are drawn independently of the production decoder and
renderer. The comparator checks dimensions, the whole page, and declared regions
without alignment or resizing. With a clean background, regional foreground
retention measures annotations separately from printed page content; whole-page
metrics still cover all pixels. The same rule rejects missing marks, shifted
marks, wrong-page content, and blank output. A generated geometry comparison
checks rendering behavior, not compatibility with Goodnotes' file formats or
complete rendering fidelity.

Require a negative control for every defect class. Run a captured known-bad
output or deliberately omit an annotation from a generated fixture, then require
the same acceptance rule to fail it. Give the omitted annotation its own region.
Never relax tolerances to make a known omission pass. Base regional limits on the
independent reference and expected rendering variation.

One bounded two-turn Luna fresh agent pilot on generated fixtures passed this
sequence: full annotated PDF export, mark edit, page insertion, notebook rename
and move, then retrieval of the same stable page ID at its new position, 3
(previously 2), through a selected bundle without another export. This single
scenario does not measure general study-answer accuracy. The default receipt
does not score that broader evaluation; report it as `unverified`. Real iPad or
TCC acceptance remains manual and `unverified`.

The receipt's journey metrics use numbered journeys and record duration, call
counts, stdout byte counts, and sanitized command/page selections. It does not
retain raw CLI responses, private names, or generated images. Keep
`annotation_status`, `image_source`, coverage, object counts, warning lists,
source labels, one-based page numbers, and unknown values in the evidence. A
successful generated comparison does not turn approximate native reconstruction
into verified full fidelity.

See the [verification feature map](../features/verification.md) for the linked
journeys and the existing safety, storage, and reader tests.
