# Architecture

The CLI reads a validated copy. `sync` copies the Goodnotes Library; commands read the copy. It never opens source SQLite/RocksDB, controls Goodnotes, or writes to the source.

```text
source -> inventory -> independent staging -> validate -> active snapshot
                         | failure                         |
                         +-> keep old pointer               +-> readers (shared .view.lock)
                                                            +-> publish/prune (exclusive .view.lock)
```

## Refresh and read lifecycle

| Phase | Rule |
| --- | --- |
| Inventory | Compare metadata for required databases, SQLite sidecars, optional native store, and attachments. Matching source and cache inventories reuse the active view without hashing all bytes. `--force` bypasses reuse. |
| Copy | Changed files go to independent inodes. macOS tries APFS clones; otherwise copy bytes. Never hardlink source or snapshot files. Reuse prior verified digests only when metadata matches; SQLite reuse requires the complete original database/sidecar family to match. |
| Validate | Consolidate copied SQLite sidecars only in unpublished staging. Validate the copied projection and write the marker last. Source and completed-copy fingerprints stay separate. |
| Retry | Allow at most three capture attempts after detected source changes. If none succeeds, fail and preserve the prior active pointer. |
| Publish | Compare initial and final inventories. A mismatch may publish the valid copy with `source_changed_during_refresh: true`; the next sync captures again. Matching checks do not promise latest source state at return. |

If a WAL changes while the main database stat signature stays the same, capture may reuse its saved source digest but still copies and checks the current main file. It never reuses the prior consolidated database for a changed SQLite family.

SQLite may accept only a committed WAL prefix. A checkpoint, backup, or integrity check cannot prove a torn or reset tail held no more edits. See [snapshot tests](../tests/test_snapshot.py).

## Mutable paths, readers, and locks

| Path | Access rule |
| --- | --- |
| Source Library | `sync` copies files; no engine opens source databases. Goodnotes does not participate in CLI locks. |
| Staging | `sync` and consolidation may write independent unpublished copies. Abandoned staging is removed on the next sync. |
| Completed snapshot | Immutable; readers open databases read-only. |
| `.sync.lock` | Serializes the full refresh. |
| `.view.lock` | Readers open the existing file read-only and hold a shared lock through their last read. Sync takes it exclusively only to publish/prune. |
| Artifact store | Separate from snapshots. Render/bundle acquire its lock after the shared view lock. |

Locks must be regular, no-follow files with stable identity. Readers create no cache paths and never recreate missing locks. A long reader delays publish/prune while capture proceeds.

## Artifact cache

Managed `render` and `bundle` outputs use a separate artifact store; explicit output paths remain caller-owned. Cleanup runs during later artifact operations, not in a background process.

| Bound | Behavior |
| --- | --- |
| 100,000,000 logical bytes | Count unique PNG inodes, manifests, and entry metadata. Exclude small control files and in-progress staging. |
| 86,400-second idle expiry | Measure from last CLI request; capacity pressure may evict older unused entries. |
| 5-minute lease | Protect returned entries from expiry and eviction. Fail if a result cannot fit without evicting protected entries. |

Render reuse checks snapshot, background/native inputs, page, layer, DPI, and renderer version before and after reuse. Cache hits do not open RocksDB. Bundles read current text; annotated PDFs keep full digest verification. Only generated PNGs may be hardlinked, within this store.

## Package boundaries

| Packages | Own |
| --- | --- |
| `models`, `errors` | Shared records and expected CLI failures. |
| `safety`, `snapshot`, `cache` | Path validation, independent capture, active pointers, locks, and pruning. |
| `library`, `text_index` | Read-only database sessions, page/text mapping, source labels, and batched index queries. `library` owns connections and result mapping. |
| `documents` | PDF backgrounds, rendering, export, and bundles. Standalone render/export default to background; study bundles default to native. |
| `annotations`, `native`, `native_assets`, `native_render` | Bounded decoders, disposable native-store reads, copied image validation, drawing, and coverage. |
| `artifacts` | Generated-output retention, deduplication, leases, and cleanup; no database reads or fidelity decisions. |
| `watch`, `diagnostics` | Metadata polling and access/runtime reporting without permission changes. |
| `cli`, `output` | Command behavior and structured response formatting. |

## Native and output limits

Native rendering uses disposable scratch copied from a validated snapshot. Fresh engine options keep writes in scratch; copied `OPTIONS` files are not loaded. Byte-copy fallback is limited to 64 MiB per extraction. A compatible RocksDB runtime is optional; `GOODNOTES_ROCKSDB_LIBRARY` and `GOODNOTES_SCRATCH_DIR` select runtime and scratch paths.

Observed support includes pen/highlighter records, filled paths, one ellipse form, variable-width pencil, one open-line form, and some linked PNG/JPEG images. Pencil texture is approximate. Unknown visibility, geometry, transforms, and records stay unknown or reduce coverage. Missing native data is unavailable, not empty. Generated tests do not prove Goodnotes compatibility or full rendering fidelity.

Public page numbers are one-based. Keep unknown/unavailable distinct from absent, false, or zero; preserve source labels, warnings, coverage, and annotation status. Annotated-PDF registration checks path, digest, and page count, but not page order or annotation inclusion; it adds no text ingestion.

Use `uv run goodnotes ...`. Command help and `goodnotes schema` define the public flags and output contract.
