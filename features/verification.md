# Verification feature map

This map connects verification goals to runnable generated tests and existing
reader or storage tests. The receipt's `generated_acceptance` status covers the
two required generated suites. The linked existing regressions remain part of
full QA; the runner does not count them as generated acceptance suites.

## Sub-features

### Generated study sessions

The study journeys cover a whole-notebook native annotated PDF export from
generated records, selected-page discussion, generated source edits, refresh, and
selected-page retrieval. They also exercise
stable page identities after insertion and reordering, notebook rename or move,
explicitly deleted pages, stale snapshots, artifact expiry, and missing visual
evidence.

- `test_study_export_discuss_edit_refresh_selected_pages`
- `test_study_stable_identity_stale_snapshot_deleted_page_and_expired_artifact`
- `test_study_visual_evidence_never_silently_falls_back`
- `test_study_supported_native_schema_matrix_matches_independent_references`
- `test_study_omitted_image_field_6_metadata_survives_render_export_and_cached_bundle`
- `test_study_unsupported_native_variants_keep_partial_mixed_page_output`
- `test_study_unsupported_only_native_variants_fail_with_page_context_and_cleanup`

These run with `uv run pytest tests/test_study_journeys.py` in
[`test_study_journeys.py`](../tests/test_study_journeys.py).

The installed-CLI matrix covers supported pen and highlighter encodings,
identity layouts, filled paths, an outlined ellipse, variable-width brush
geometry, two-point open lines and bounded line translation, and three exact
image binding layouts, including a clipped page-edge image, with body field 6
present or absent. Every generated mark
has an independent PDF reference and an omission control for its own pixel
region. The omitted-image-field case preserves its count and warning through
render, export, cached bundle reuse, and persistent bundle manifests. A stale
renderer-version cache seeded with a background-only image must be rejected.
Separate controls retain partial output beside unknown records and fail
unsupported-only pages with page context and clean output state.

### Independent rendering references

Generated expected pages are drawn independently of native decoding and
production rendering. The comparator tests dimensions, whole-page and regional
content, page selection, and rejection of missing, shifted, wrong-page, or blank
output.

For each private case, the checker renders one clean background from the copied
page and reuses it for the native render and bundle comparisons. Regional
foreground retention excludes printed page content, while whole-page metrics
still cover every pixel. The version 1 manifest has no background field.

- `test_generated_native_output_matches_independently_drawn_reference`
- `test_same_rule_rejects_missing_shifted_wrong_page_and_blank`
- `test_png_and_pdf_and_page_selection_have_the_same_rule`
- `test_dimension_mismatch_is_not_resized_and_empty_reference_region_fails`
- `test_blank_cannot_pass_even_with_permissive_tolerances`
- `test_clean_background_excludes_dense_printing_with_per_mark_regions`
- `test_clean_background_dimensions_must_match`

Run them with `uv run pytest tests/test_verification_reference.py` in
[`test_verification_reference.py`](../tests/test_verification_reference.py).
That file also tests bounded input validation, clean-background handling, and
redaction and cleanup for an explicit private corpus.

### Receipt, target, and privacy checks

[`test_verification_runner.py`](../tests/test_verification_runner.py) exercises
the receipt contract and target selection:

- `test_receipt_has_source_provenance_and_sanitized_journey_aggregates`
- `test_tree_change_during_acceptance_invalidates_receipt`
- `test_required_suite_skip_is_blocked_and_subprocess_text_is_not_reported`
- `test_untracked_source_config_changes_fingerprint`
- `test_private_result_keeps_only_aggregate_and_opaque_fingerprint`
- `test_installed_target_rejects_same_version_with_stale_package_and_space_path`
- `test_report_does_not_overwrite_an_unrelated_existing_file`

Run them with `uv run pytest tests/test_verification_runner.py`.

### Existing safety, storage, and reader behavior

The generated verification journeys reuse the project test fixtures and reader
contracts. These existing tests cover the surrounding behavior without adding a
second QA framework:

- Source capture and copied-WAL handling: `test_snapshot_consolidates_copied_wal_and_reads_committed_rows` in [`test_snapshot.py`](../tests/test_snapshot.py).
- Snapshot publication and stale generations: `test_old_reader_holds_snapshot_while_capture_progresses_and_publication_waits` and `test_expected_snapshot_mismatch_raises_stale_snapshot_error` in [`test_cache.py`](../tests/test_cache.py).
- Search labels and literal selectors: `test_search_uses_literal_casefolded_terms_scope_and_source_labels` in [`test_library.py`](../tests/test_library.py).
- Read-only native output: `test_native_png_pdf_bundle_preserve_sources_and_readonly_cache` in [`test_native_workflow.py`](../tests/test_native_workflow.py).
- Annotated evidence must not silently fall back: `test_annotated_layer_never_falls_back_to_background` in [`test_documents.py`](../tests/test_documents.py).

Run the linked cases with:

```sh
uv run pytest \
  tests/test_snapshot.py::test_snapshot_consolidates_copied_wal_and_reads_committed_rows \
  tests/test_cache.py::test_old_reader_holds_snapshot_while_capture_progresses_and_publication_waits \
  tests/test_cache.py::test_expected_snapshot_mismatch_raises_stale_snapshot_error \
  tests/test_library.py::test_search_uses_literal_casefolded_terms_scope_and_source_labels \
  tests/test_native_workflow.py::test_native_png_pdf_bundle_preserve_sources_and_readonly_cache \
  tests/test_documents.py::test_annotated_layer_never_falls_back_to_background
```

### Installed distribution

The macOS CI test job builds a wheel, installs it into a separate environment
outside the checkout, and invokes the generated journeys with its absolute
`goodnotes` executable. The `Verify installed study journeys` step in
[`ci.yml`](../.github/workflows/ci.yml) uses the same runner as local
verification. The package job's schema smoke check remains a separate packaging
check.

## How to get to it (user POV)

- To check the generated workflow against the repository environment, run
  `uv run python scripts/verify.py`.
- To check an installed wheel, install it outside the checkout and pass its
  absolute `goodnotes` executable with `--cli`.
- To compare a pre-existing private copy with independently exported references,
  keep the manifest and all referenced files outside the checkout, then pass the
  manifest with `--private-corpus`.
- To run the wider regression set, use the separate full `pytest`, Ruff, and `ty`
  checks in [Contributing](../CONTRIBUTING.md).

## Driving it with `verify.py`

The default runner requires the generated study and reference suites. It writes
`.local/verification/report.json`; `--report` selects a different receipt path.
`--cli` selects an absolute installed executable. `--private-corpus` requests the
opt-in copied-snapshot comparison. The runner records the source commit, dirty
state, and source content fingerprint; it checks that Git status and content
remain stable and reports the selected target, environment, checks executed,
and their statuses. Every required generated test must pass with zero skips;
otherwise the acceptance result is blocked or failed.

Feature-specific commands and evidence limits are in
[development verification](../docs/verification.md). The workflow points back
here instead of restating each existing safety and reader test.

## Gotchas

- A required generated test that skips because RocksDB is unavailable is
  `blocked`, and the verifier exits nonzero. A regular pytest skip by itself is
  not a passing acceptance result.
- Page numbers are current positions. Carry page IDs across edits, then resolve
  their current positions. Treat a deleted identity as missing.
- A declared `Goodnotes-export` reference is trusted metadata; the verifier does
  not authenticate which application produced the PDF.
- Give each required small mark its own region. A large stroke or highlight can
  dominate a broad region's pixel metrics and hide a missing thin mark. Keep the
  whole-page check, then require each mark's region to reject its omission.
- Use a clean background render to separate annotations from printed page
  content when measuring regional retention. Whole-page metrics include both.
- Require a known-bad control or deliberate generated omission for every defect
  class. The same acceptance rule must fail it. Keep reference-derived thresholds
  unchanged; do not auto-accept a defect by loosening them.
- Geometry agreement against a generated PDF checks coordinate and rendering
  behavior. It does not establish universal Goodnotes compatibility or full
  fidelity. Preserve native coverage, counts, warnings, and unknown values.
- One bounded two-turn Luna fresh agent pilot on generated fixtures passed a
  full annotated PDF export, mark edit, page insertion, and notebook rename and
  move. It followed the same stable page ID from position 2 to 3 and retrieved
  its selected bundle without exporting again. This does not measure general
  agent answer accuracy; the default receipt does not score that, so report it
  as unverified.
- Actual iPad/TCC acceptance remains manual and unverified. A deterministic
  subprocess journey does not establish it.
- Optional private checks that were not requested remain `unverified`. When run,
  the receipt includes aggregate outcomes and opaque fingerprints, not notebook
  selectors, private paths, note text, command output, or image diffs.
