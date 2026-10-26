# Development verification

`scripts/verify.py` runs generated study journeys and image-reference checks. It is a development tool, not a production CLI feature, and adds no runtime dependency.

## Run verification

| Goal | Command |
| --- | --- |
| Generated acceptance suites | `uv run --locked python scripts/verify.py` |
| Installed CLI | Add `--cli /absolute/path/to/goodnotes` |
| Copied private reference corpus | Add `--private-corpus /outside/repository/manifest.json` |
| Receipt location | Add `--report PATH`; default is `.local/verification/report.json`. |

The runner requires `tests/test_study_journeys.py` and `tests/test_verification_reference.py` to pass with zero skips. Generated fixtures do not read a private Library. A required skip, including unavailable native support, is `blocked` and makes the command fail.

This focused runner does not replace the full test, Ruff, and `ty` checks in [Contributing](../CONTRIBUTING.md). CI runs the journeys from source and through a wheel installed outside the checkout on macOS.

## Receipt status and provenance

| Status | Meaning |
| --- | --- |
| `pass` | The selected check ran and met its acceptance rule. |
| `fail` | A check found a mismatch, the tree changed, or receipt writing failed. |
| `blocked` | A requested check could not run to completion. |
| `unverified` | Optional evidence was not requested or exercised. |

The receipt records the source commit, dirty state, content fingerprint, target, environment, selected checks, executed checks, and journey metrics. The runner compares source contents and Git status before and after the run. Report the commit and fingerprint with any claim about what passed.

With `--cli`, the runner invokes an absolute executable outside the checkout and removes `PYTHONPATH`. It resolves the console script's interpreter and verifies that the imported package is outside the checkout and matches the current source package. The receipt records version and package fingerprints, not local executable or import paths.

CI's macOS job runs generated journeys through an installed wheel, including schema-matrix cases and failure controls. The package job's `goodnotes schema` smoke check is separate and does not replace those journeys. CI uses generated PDFs; private lecture exports and snapshots are not included.

## Receipt path restrictions

`--report` may target the default `.local/verification/report.json`, an untracked path under repository `.local/`, or a path outside the repository. The runner rejects tracked repository paths and repository paths outside `.local/`.

It also rejects a destination inside or below an ancestor containing `goodnotes-snapshot.json` or a `Databases` directory. This prevents writing into a snapshot or Library. The default receipt is allowed only when repository `.local/` is not a symlink and resolves inside the checkout.

An existing destination is replaced only if it is a receipt with schema `goodnotes-verification/v1`. Unrelated files remain untouched. Report-path protections are covered by [`tests/test_verification_runner.py`](../tests/test_verification_runner.py).

## Private reference manifest

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

## Comparison rules and limits

For every private case, the runner renders one clean background, one native page image, and one native bundle image. It compares both native images with the declared reference page. Dimensions must match exactly; the comparator does not align or resize images.

The clean background excludes printed page content only from regional foreground-retention measurements. Whole-page measurements include all pixels. Direct `compare_reference` calls may provide `background` and `background_page`; without a background, regional foreground uses a white-background rule. Raster inputs are limited to 8,000,000 pixels.

Give each required annotation its own region, especially each small mark. A large stroke or highlight can hide a missing mark in a broad region. Keep the whole-page check and require every mark's region to reject its omission.

Use a known-bad output or deliberately omit a generated annotation as a negative control for each defect class. The same acceptance rule must fail it. Do not loosen thresholds to make an omission pass.

The runner uses only the marked copy through `--sandbox`. It does not register PDFs, open source SQLite or RocksDB databases, or control Goodnotes. Temporary CLI data, native scratch, and images are removed after each case.

The corpus fingerprint covers the manifest, reference PDFs, and snapshot-marker bytes. If any changes during verification, the result fails. The aggregate receipt stores case outcomes and opaque fingerprints. It omits selectors, private paths, note text, CLI output, and image diffs. Without `--private-corpus`, private-corpus evidence is `unverified`.

## What the evidence establishes

Generated journeys exercise annotated export, selected-page discussion, a generated edit and sync, and retrieval of selected current pages. They also cover page identity after insertion, reordering, rename, or move; missing and deleted pages; stale snapshots; artifact expiry; and unavailable native or annotated evidence. Page positions are not durable identities.

The schema matrix compares observed generated pen, highlighter, path, ellipse, brush, open-line, and image cases with independent references. Omission controls must fail their region. Cache invalidation, native counts and warnings, and partial bundles are also checked.

Generated references test geometry against independent drawings. They do not establish support for every Goodnotes format, full native fidelity, general study-answer accuracy, or fresh iPad/TCC acceptance. iPad/TCC acceptance remains manual and `unverified`.

Journey metrics include numbered journeys, duration, call counts, stdout byte counts, and sanitized command/page selections. Receipts omit raw CLI responses, private names, and generated images. Preserve `annotation_status`, `image_source`, coverage, object counts, warnings, source labels, one-based pages, and unknown values when interpreting results.

For the scenario-to-test map, see [Verification tests](verification-tests.md).
