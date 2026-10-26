# Verification test map

Use this map to find the tests for a behavior. The focused verifier requires the
first two suites. The full test run also includes the regressions below.

## Required generated acceptance suites

| Suite | What it checks |
| --- | --- |
| [Study journeys](../tests/test_study_journeys.py) | Export, edit, refresh, selected-page retrieval, stable page identity, native schema cases, image omissions, partial output, and failed visual evidence. |
| [Reference comparisons](../tests/test_verification_reference.py) | Independent drawings, dimensions and page selection, whole-page and per-mark thresholds, omission controls, private manifest validation, redaction, cleanup, and input stability. |

## Supporting regression suites

| Behavior | Tests |
| --- | --- |
| Receipt provenance, required skips, installed targets, and safe report destinations | [Verification runner](../tests/test_verification_runner.py) |
| Independent copies, copied WAL consolidation, source preservation | [Snapshots](../tests/test_snapshot.py) |
| Reader locks, publication, stale generations, retention | [Cache](../tests/test_cache.py) |
| Text-source labels, PDF bindings, search coverage, missing indexes | [Library](../tests/test_library.py) |
| Native rendering with read-only copies, refresh, and watch | [Native workflows](../tests/test_native_workflow.py) |
| Artifact limits, leases, deduplication, safe cleanup | [Artifacts](../tests/test_artifacts.py) |
| Render reuse with fresh text, persistent exports, renderer invalidation | [Storage workflows](../tests/test_storage_workflow.py) |
| Annotated PDF count/signature checks and no silent fallback | [Documents](../tests/test_documents.py) |
| Source-labelled output, bounded text, partial bundle rows | [Agent output](../tests/test_agent_output.py) |
| Generated CLI reference, broken docs links, and navigation omissions | [Documentation](../tests/test_docs.py) |

For commands and exact acceptance rules, see [development verification](verification.md).
Generated tests check their declared fixtures. They do not establish universal
Goodnotes compatibility, complete native rendering fidelity, or fresh iPad/TCC acceptance.
