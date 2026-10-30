# Development verification

## Run verification

| Goal | Command |
| --- | --- |
| Generated acceptance suites | `uv run --locked python scripts/verify.py` |
| Installed CLI | Add `--cli /absolute/path/to/goodnotes` |
| Copied private reference corpus | Add `--private-corpus /outside/repository/manifest.json` |
| Receipt location | Add `--report PATH`; default is `.local/verification/report.json`. |

The runner requires `tests/journeys/test_study_journeys.py` and `tests/verification/test_verification_reference.py` to pass with zero skips. Generated fixtures do not read a private Library. A required skip, including unavailable native support, is `blocked` and makes the command fail.

This focused runner does not replace full tests, Ruff, or `ty` ([Contributing](../CONTRIBUTING.md)). CI runs generated journeys from source and an installed macOS wheel; its separate `goodnotes schema` smoke check is not a substitute. CI uses generated PDFs only.

The test-only architecture comparator records timed-out cases as incomplete, including captured
stdout/stderr decoded with UTF-8 replacement for invalid bytes. Matching timeouts cannot establish
equivalence. Its exit status reports capture completion; inspect `comparison_state`,
`incomplete_cases`, and completed-case differences before making a behavior-preservation claim.

## Receipt status and provenance

| Status | Meaning |
| --- | --- |
| `pass` | The selected check ran and met its acceptance rule. |
| `fail` | A check found a mismatch, the source or target changed, or receipt writing failed. |
| `blocked` | A requested check could not run to completion. |
| `unverified` | Optional evidence was not requested or exercised. |

The receipt records the source commit, dirty state, content fingerprint, target, environment, selected checks, executed checks, and journey metrics. The runner compares source contents, Git status and selected target identity before and after the run. A passing receipt requires both `tree_integrity.status` and `target_integrity.status` to be `pass`. Report the commit and fingerprint with any claim about what passed.

With `--cli`, the runner invokes an absolute executable whose resolved location is outside the checkout and isolates inherited Goodnotes configuration, Python and pytest settings. It preserves an explicitly selected `GOODNOTES_ROCKSDB_LIBRARY` and applies the requested verification target controls.

The selected interpreter must import an external package matching current source. Its installed distribution must declare `goodnotes = goodnotes_cli.cli:main` and contain one RECORD entry for the resolved executable with a matching SHA256 hash. The actual executable's `schema` must then report the same version and full source fingerprint. Missing or unsupported metadata and launcher forms block verification. Interpreter discovery supports Python shebangs and the observed uv shell trampoline for environment paths containing spaces; discovery reads the header without executing shell code.

After all selected checks, the runner revalidates the original CLI selection, including a supplied symlink alias. A changed resolved target, launcher, interpreter, package or reported build fails target integrity. A known change remains a failure if a later validation step cannot complete; unavailable revalidation without a proven change is `blocked`. Receipts store version, opaque launcher/package fingerprints and the binding basis, without executable, interpreter or import paths or raw probe output. These checks establish local installation consistency; local RECORD metadata and command output are not adversarial authentication.

The default target verifies the prepared source-editable environment. An explicit external editable installation can qualify if its package is outside this checkout and all binding checks pass. This does not establish wheel or sdist installation evidence; use the fresh-artifact workflow below for that claim.

## Installed build and snapshot provenance

`goodnotes --version` prints `VERSION (source FIRST12SHA)`. The schema index and targeted schema
include `build.version` and the full `build.source_sha256`; `doctor` includes `provenance.cli` with
the same build identity. The fingerprint covers sorted package-relative Python source files,
including vendored modules, so two builds with the same package version can still be distinguished
without exposing an installation path.

New snapshots keep marker `format_version: 1` and add a producer identity, optional installed-app
metadata, and SQLite projection format values. `source_app` is populated only when capture uses
the default Goodnotes Library path and a matching installed app `Info.plist` is available. Its
`basis` is `installed-app-at-capture`; it describes installed bundle metadata and does not claim
which app version originally wrote every Library file. Explicit source copies and unavailable app
metadata record `source_app: null`. The SQLite `user_version` and `application_id` values are read
from the copied projection database; they are SQLite format identifiers, not the Goodnotes app
version. `doctor.provenance` reports `snapshot_format`, `producer`, `source_app`, and
`library_format` from the active snapshot marker. Historical format-1 markers without these optional
fields remain valid; `doctor` reports those unknown values as `null`.

For a fresh wheel check, build and install into a new environment outside the checkout, then pass
that environment's executable to the runner:

```sh
uv build --wheel --out-dir /absolute/path/outside/repository/wheelhouse
uv venv /absolute/path/outside/repository/install-venv
uv pip install --python /absolute/path/outside/repository/install-venv/bin/python \
  /absolute/path/outside/repository/wheelhouse/*.whl
uv run --locked python scripts/verify.py \
  --cli /absolute/path/outside/repository/install-venv/bin/goodnotes \
  --report /absolute/path/outside/repository/installed-report.json
```

The runner requires that installed executable to resolve its package outside the checkout and
match the current source. A basic setup smoke check is `goodnotes --version` and `goodnotes schema`
from that installed environment. `uv sync --locked` prepares the project-local `.venv` by default;
`UV_PROJECT_ENVIRONMENT` can select another environment path. Set `UV_CACHE_DIR` to a writable
package-cache path when needed; it is separate from the project environment, Goodnotes CLI's
`--data-dir` or `GOODNOTES_DATA_DIR`, and native rendering's `GOODNOTES_SCRATCH_DIR`.
`uv run --locked --no-sync` is only for checks in an already prepared environment. It skips
dependency synchronization and does not install or verify dependencies.

## Receipt path restrictions

`--report` may target the default `.local/verification/report.json`, an untracked path under repository `.local/`, or a path outside the repository. The runner rejects tracked repository paths and repository paths outside `.local/`.

Repository-local admission requires a successful literal Git lookup proving the destination is untracked. Git errors, inability to start and timeouts fail before directories or temporary receipts are created.

It also rejects a destination inside or below an ancestor containing `goodnotes-snapshot.json` or a `Databases` directory. This prevents writing into a snapshot or Library. The default receipt is allowed only when repository `.local/` is not a symlink and resolves inside the checkout.

An existing destination is replaced only if it is a receipt with schema `goodnotes-verification/v1`. Unrelated files remain untouched. Report-path protections are covered by [`tests/verification/test_verification_runner.py`](../tests/verification/test_verification_runner.py).

## Private manifest and comparison rules

Keep the manifest, marked snapshot, and independently exported Goodnotes PDFs outside the repository. Manifest paths can be absolute or relative to the manifest directory. The runner does not capture or sync a live Library.

| Manifest rule | Limit |
| --- | --- |
| Top-level object | Exactly `version` and `cases`; `version` is integer `1`. |
| Manifest size | At most 256,000 bytes. |
| Cases | 1 to 20. |
| Referenced files | Regular non-symlink files, at most 64 MiB each. Snapshot directories must be non-symlink and contain `goodnotes-snapshot.json`. |

Each case contains exactly the required fields below. `dpi` is the only optional field.

| Field | Contract |
| --- | --- |
| `sandbox` | Path to a pre-existing marked snapshot copy. |
| `notebook` | Non-empty exact selector for a notebook in that copy. |
| `page` | One-based integer from 1 to 1,000. |
| `reference` | Path to an independently exported PDF. |
| `reference_page` | One-based integer from 1 to 1,000, within the PDF. |
| `reference_provenance` | Exact string `Goodnotes-export`. This is declared metadata, not an authenticated producer check. |
| `regions` | 1 to 32 regions. Each has exactly `box`, `max_mean_error`, `max_changed_fraction`, and `min_foreground_retention`. |
| `pixel_tolerance` | Maximum per-channel RGB difference as a fraction from 0 to 1. |
| `dpi` | Optional integer 72 to 300; defaults to 144. |

Each `box` is exactly four finite fractions `[x0, y0, x1, y1]` in the normalized 0-to-1 page range. Require `x0 < x1` and `y0 < y1`. The three limits and `pixel_tolerance` are finite fractions from 0 to 1. Extra, missing, or misspelled fields invalidate the manifest.

The whole-page check uses the strictest regional limits: the minimum `max_mean_error`, minimum `max_changed_fraction`, and maximum `min_foreground_retention`. Each region must contain foreground under the selected background rule.

For every private case, the runner renders one clean background, one native page image, and one native bundle image. It compares both native images with the declared reference page. Dimensions must match exactly; the comparator does not align or resize images.

The clean background excludes printed content only from regional foreground-retention measurements. Whole-page checks include all pixels. Raster inputs are limited to 8,000,000 pixels.

Give each required annotation its own region, especially each small mark. A large stroke or highlight can hide a missing mark in a broad region. Keep the whole-page check and require every mark's region to reject its omission.

Use a known-bad output or deliberately omit a generated annotation as a negative control for each defect class. The same acceptance rule must fail it. Do not loosen thresholds to make an omission pass.

The runner uses only the marked copy through `--sandbox`. It does not register PDFs, open source SQLite or RocksDB databases, or control Goodnotes. Temporary CLI data, native scratch, and images are removed after each case.

The corpus fingerprint covers the manifest, reference PDFs, and snapshot-marker bytes. If any changes during verification, the result fails. The aggregate receipt stores case outcomes and opaque fingerprints. It omits selectors, private paths, note text, CLI output, and image diffs. Without `--private-corpus`, private-corpus evidence is `unverified`.

### Expand a private reference

Keep each version outside the checkout with its frozen snapshot, independent PDF export, manifests and receipts. Preserve old versions. A new capture requires explicit authorization and the file-copy-only workflow; verification never refreshes the copy. Match notebook and reference page order.

Exercise combinations of disconnected/repeated points, open/closed paths, curves, transforms, opacity, fills, patterns, images and nearby text. Add an omission control for every defect class. Keep strict pixel results separate from qualitative checks that allow pencil texture differences. Do not loosen thresholds to hide missing content. Add generated fixtures for established format rules and keep real notebook contents private.

## What the evidence establishes

Generated journeys test selected-page retrieval across edits, page movement/deletion, stale snapshots, artifact expiry, and unavailable native evidence. The schema matrix compares observed object layouts with independent references; omission controls must fail their region. See [study journeys](../tests/journeys/test_study_journeys.py) and [reference checks](../tests/verification/test_verification_reference.py).

Generated references test geometry against independent drawings. They do not establish universal Goodnotes compatibility, full native fidelity, general study-answer accuracy, or fresh iPad/TCC acceptance. iPad/TCC remains manual and `unverified`.

Journey metrics include numbered journeys, duration, call counts, stdout byte counts, and sanitized command/page selections. Receipts omit raw CLI responses, private names, and generated images. Preserve `annotation_status`, `image_source`, coverage, object counts, warnings, source labels, one-based pages, and unknown values when interpreting results.
