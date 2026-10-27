# Architecture refactor acceptance

This work implements the architecture audit against main at `0dc8977`. The goal is readable,
cohesive modules and explicit resource ownership within the existing distribution. Smaller files
alone do not establish a better design. Production declarations, compatibility exports and imports
count toward the code-volume comparison.

## Contracts

- Preserve command registration, flags, help, schema, default compact JSON, ordering, source labels,
  warnings, null versus missing values, exit codes and one-based page numbers.
- Preserve successful default and verbose outputs. Source provenance changes when package source
  changes and must continue to describe the actual installed package.
- Keep source libraries and completed snapshots immutable. Engines open validated copies only.
  SQLite consolidation may write only to independently copied, unpublished staging files.
- Keep reader view locks through all database and PDF work. Acquire artifact locks after the view
  pin and retain artifact leases through output emission.
- Keep distinct policies for background paths, native image resources, registered PDFs, capture
  inventories and artifact retention. Similar-looking validators do not imply interchangeable rules.
- Retain console `goodnotes_cli.cli:main`, module execution and supported Library/native exports.
  Tests and package internals use the owning module rather than compatibility patch proxies.

## Ownership and organization

`cli` adapts Typer commands to domain operations. `presentation` owns output metadata, projection,
serialization and selections. Typer remains the source of command and parameter metadata.

`reader` separates catalog navigation, page hydration, index mapping, bounded text, search, PDF
fallback and diagnostics. Each Library opening creates fresh collaborators. ReaderSession alone owns
SQL connections and the session PDF LRU. Closing attempts every PDF handle and releases SQL even if
PDF cleanup fails. Failed optional-index setup closes provisional resources and remains retryable.

`storage` separates validated paths and locks from cache refresh, independent capture and artifact
transactions. ArtifactCache owns stages, pins, records and publication. Its private filesystem owner
holds descriptors and the exclusive lock. Helpers borrow those resources and never outlive the
session. Validation at deletion remains mandatory.

`native` separates runtime and scratch-store access from decoded records, values, ink, paths,
object dispatch, resource binding and raster validation. One page budget includes rejected objects.
`rendering` separates backgrounds, registered PDFs, page rendering, cache identity, export, overlay,
SVG and publication. Native output never silently substitutes a background.

Substantial modules normally fit in 150–450 lines. Larger cohesive implementations require an
explicit review; no giant implementation may simply move intact. The Library facade should stay
below roughly 250 lines. No mixins, service locators, generic command framework, shared god context
or dynamic forwarding compatibility layer is part of this design.

## Selected changes to exceptional outcomes

The audit accepted these fixes in addition to mechanical movement:

| ID | Required result |
| --- | --- |
| F01 | Iterative catalog graph traversal supports deep valid graphs and avoids repeated parent walks. |
| F02 | Optional FTS setup commits ownership only after schema and casefold registration succeed. |
| F03 | All PDF closes are attempted; the first cleanup failure is retained with deterministic chaining. |
| F04 | Stage and publication initialization failures clean only state created by that operation. |
| F05 | Internal control flow uses structured reasons while preserving public messages and priorities. |
| F06 | SQL parameter budgets include every bind in the statement. |
| F07 | Zero-budget attachment checks are confined; prefixed direct paths precede basename fallback without changing literal direct-path priority. |
| F08 | Trusted platform path aliases are recognized only after identity verification. |
| F09 | Blob temporary-file cleanup covers parent acquisition failures after a successful copy. |
| F10 | Error classification follows explicit causes, honors suppressed context and guards cycles. |
| F11 | Overlay types describe image requirements without widening the minimal native store interface. |
| F12 | Cache metadata reads are bounded, no-follow regular-file reads with identity checks. |
| F13 | Requested bundle document_id is meaningful; missing or mixed evidence identity fails before publication, and default/verbose shapes stay stable. |
| F14 | Unsupported per-page native background mappings allow partial bundles; full exports and all-failed bundles remain strict. |
| F15 | Truncation flags remain consistent, and conflicting doctor modes have deterministic error precedence. |
| F16 | Verification catches Git timeout failures, isolates inherited configuration and avoids repeated work. |
| F17 | Wire field diagnostics distinguish zero and out-of-range values; TPL string consumption stays unchanged. |

Keep uncertain WAL failures closed, session PDF reuse, exact selectors, ID ambiguity, dynamic source
fingerprints and existing bounded/unbounded NUL semantics. No new native schema support, OCR,
background fallback or speculative PDF-origin correction is included. Late source-mutation errors
may leave already-published output under the existing contract; a concurrent replacement cannot be
safely rolled back with a stat-then-unlink check.

## Verification

Use generated fixtures, a frozen baseline comparison, targeted failure injection and independently
encoded native/reference data. Reorganize tests with their modules and shared generated support;
keep acceptance discovery, dynamic imports, documentation and CI consistent.

Required gates are the full test suite, Ruff formatting and lint, ty, strict MkDocs, generated
verification journeys, and wheel plus source-distribution installation outside the checkout.
Optional native support must be exercised with zero required skips where available. Generated tests
do not establish new Goodnotes fidelity or manual iPad/TCC acceptance.

Independent correctness and maintainability review must resolve material findings. Review the final
diff separately against documented standards and this specification. Report actual production code
and physical-line deltas, gross additions/deletions and forwarding definitions. Performance claims
need measurements; conditional optimization and portability hypotheses are not required fixes.

## Implementation measurements and decisions

The first implementation was compared against the frozen `0dc8977` tree using 124 generated cases
across all 16 commands. The comparison covered help/schema, flags, compact/verbose/field projections,
errors, ordering, warnings, source labels, generated SQLite/FTS/PDF content and output files. Only
F13/F15 differences were observed. Separate native fixtures exercise F14. This is selected generated
evidence, not proof of every possible input or native format.

| First-party package metric | Main | Refactor |
| --- | ---: | ---: |
| Python modules, including compatibility exports | 20 | 86 |
| Physical lines | 15,733 | 17,481 |
| Nonblank, noncomment, nondocstring lines | 13,897 | 15,283 |
| Lines occupied by import statements | 316 | 1,012 |
| Function definitions, including nested/protocol methods | 496 | 551 |
| Classes, including typed records | 55 | 86 |
| Functions consisting of one delegating call | 18 | 25 |
| Largest module | 2,521 | 616 |

These totals include fixes, types, import overhead and compatibility exports; vendor, tests and
scripts are excluded. They are AST/source counts, not performance measurements. Import spans include
multiline imports. The delegating-call heuristic includes useful public facade methods and is not a
count of defects. Source relocation and fixes were integrated together, so no reliable mechanical-only
line delta is claimed. The combined diff and focused regressions expose the actual behavior changes.

The original no-growth target was missed: code grew by 1,386 lines, or 10.0%. Of that increase,
696 lines are import spans. The remaining increase includes explicit session and filesystem ownership,
owner-local result types, bounded metadata and failure cleanup. This is an explicit deviation from
the target, not a claimed reduction. Shared bounded text materialization, count queries, session
checks and duplicate reset state were consolidated. We kept substantive safety checks and independent
test encoders. We did not compress source formatting or remove types to make the count smaller.

The 616-line text-index module includes its typed result contracts, verified PDF binding and coverage
queries. The artifact filesystem and transaction modules are approximately 580 and 560 lines; they
retain one descriptor owner and one mutation owner. The 569-line native asset module keeps resource
binding and stable reads together, with raster parsing separate. These cohesive exceptions received
independent review. Further splitting solely to meet a numeric file cap would add more interfaces.

Retention accounting was measured on generated 8/16/32-entry workloads. The evidence did not justify
an incremental accounting subsystem, so its existing capacity and identity policies remain. Broader
permission/ACL and clone-errno changes, global source-hash caching, uncertain WAL fallback, selector
normalization and new native schema support remain outside this change. Before a future release tag,
promote the changelog with the real release date; this refactor does not create a release.

The local full suite passed with native support required: 955 passed and one case-sensitive-filesystem
skip. Formatting, lint, types, strict docs, source verification and isolated wheel/sdist smoke checks
passed. Source verification ran 102 required acceptance cases with zero skips. Manual private-corpus,
iPad and TCC acceptance remains unverified.
