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
| Publish | Compare initial and final inventories. A mismatch may publish the valid copy with `source_changed_during_refresh: true`; the next sync captures again. The flag covers this refresh window only; `false` does not mean no edits occurred since the previous sync. Matching checks do not promise latest source state at return. |

`checked_at` records the latest check. `captured_at` records when the active
snapshot was created; with `status: unchanged`, the prior snapshot is reused, so
`captured_at` can be older than `checked_at`.

If a WAL changes while the main database stat signature stays the same, capture may reuse its saved source digest but still copies and checks the current main file. It never reuses the prior consolidated database for a changed SQLite family.

SQLite may accept only a committed WAL prefix. A checkpoint, backup, or integrity check cannot prove a torn or reset tail held no more edits. See [snapshot tests](../tests/storage/test_snapshot.py).

## Mutable paths, readers, and locks

| Path | Access rule |
| --- | --- |
| Source Library | `sync` copies files; no engine opens source databases. Goodnotes does not participate in CLI locks. |
| Staging | `sync` and consolidation may write independent unpublished copies. Abandoned staging is removed on the next sync. |
| Completed snapshot | Immutable; readers open databases read-only. |
| `.sync.lock` | Serializes the full refresh. |
| `.view.lock` | Readers open the existing file read-only and hold a shared lock through their last read. Sync takes it exclusively only to publish/prune. |
| Artifact store | Separate from snapshots. Managed render/bundle hold its lock through response delivery, after acquiring the shared view lock. |

Locks must be regular, no-follow files with stable identity. Readers create no cache paths and never recreate missing locks. A long reader delays publish/prune while capture proceeds.

## Artifact cache

Managed `render` and `bundle` outputs use a separate artifact store; explicit output paths remain caller-owned. Cleanup runs during later artifact operations, not in a background process.

| Bound | Behavior |
| --- | --- |
| 100,000,000 logical bytes | Count unique PNG inodes, manifests, and entry metadata. Exclude small control files and in-progress staging. |
| 86,400-second idle expiry | Measure from last CLI request; capacity pressure may evict older unused entries. |
| 5-minute lease | Protect returned entries from expiry and eviction. Fail if a result cannot fit without evicting protected entries. |

Render reuse checks snapshot, background/native inputs, page, layer, DPI, and renderer version before and after reuse. Cache hits do not open RocksDB. Bundles read current text; annotated PDFs keep full digest verification. Only generated PNGs may be hardlinked, within this store.

Managed commands renew pinned entries before delivery and again when the artifact session exits.
If final renewal fails after response bytes were written, the command exits nonzero and reports
the error on stderr. Consumers must require a successful exit before using a returned path.

## Package boundaries

| Modules | Own |
| --- | --- |
| `models`, `errors`, `config` | Shared public records, expected failures and immutable path/option selection. |
| `cli`, `presentation` | Typer adapters and command metadata; output contracts, projection, serialization and recommendations. |
| `library`, `reader` | Public Library facade; fresh per-open catalog, page, index, text, search, scope and coverage collaborators. ReaderSession owns SQL connections and the session PDF LRU. |
| `storage.paths`, `storage.sandbox`, `storage.locks`, `storage.inventory` | Containment, immutable-copy validation, reader/writer lock rules and fast refresh metadata. |
| `storage.capture` | Independently copied staging, content inventory, copied-only SQLite consolidation and snapshot publication. |
| `storage.cache` | Active-pointer validation, refresh/read-view orchestration, generation pruning and lock lifetime. |
| `storage.artifacts` | Generated-output transactions, retained records, leases and accounting. Its private filesystem owner holds descriptors and the artifact lock; helpers borrow them within the session. |
| `native` | Optional ABI/runtime, disposable store reads, bounded values/ink/path/object decoding and copied-image/raster validation. |
| `rendering` | Background and registered-PDF handles, native overlay/SVG, page rendering, managed cache, bundle, export and no-overwrite publication. |
| `watch`, `diagnostics` | Metadata polling and access/runtime reporting without permission changes. |
| `_vendor` | Attributed bounded wire, TPL and compression parsers. |

A Library open creates fresh collaborators and one ReaderSession. The session closes PDFs before
its index and projection connections; every PDF close is attempted even when one fails. Optional
index initialization owns a provisional connection until schema and casefold setup succeed. Readers
borrow PDF handles until Library closes. The caller's shared view lock encloses this full lifetime.
Reader algorithms return domain records; the public Library boundary formats coverage recommendations.

ArtifactCache enters its private filesystem owner once and keeps all borrowed descriptors inside the
session. Stages, pins and publication state belong to the transaction owner. Retention accounting
passes known directory identities through deletion. Native scratch encloses every engine and iterator handle.

Snapshot markers contain the captured file inventory and are limited to 64 MiB. If the generated
marker exceeds this limit, capture fails before publication and removes its staging copy; the
previous active snapshot remains available.

Metadata readers classify excessive JSON nesting as invalid input. Annotated-PDF registry records
use a stable, no-follow read capped at 1 MiB and preserve strict UTF-8 decoding.

Capture statistics count planned source inputs. The final snapshot manifest describes the copied
files after SQLite sidecar consolidation, so its file and byte totals can differ from those inputs.

See [refactor acceptance](architecture-refactor.md) for the reviewed scope and explicit changes to
exceptional outcomes. Existing late registered-source mutation errors can leave an already-published
output; publication does not provide rollback against a concurrent path replacement.
Bundle publication pins its parent and reserved directory, links relative to that directory, and
rolls back only entries with the identities it created. These checks reject observed replacements;
the operating system does not make separate identity-check and unlink/rmdir calls atomic against
arbitrary same-user mutation.
Its staging parent and source directory remain pinned through handoff, including cleanup after
render failure. Rollback owns only links whose calls returned successfully. An interrupted link
has an uncertain outcome and remains untouched, even if it matches the source inode. The command
fails; inspect and explicitly remove incomplete output or choose a new destination before retrying.

## Native and output limits

Library root selection is reader configuration outside immutable snapshots. A managed
cache can store `library-root.json`; explicit snapshots only use an explicit root
override. Readers validate this bounded regular file without creating or changing it.
The selected root filters catalog links and records before graph construction and
scopes text queries and page-index diagnostics. Template/pattern reference totals
remain labelled as covering the entire copied catalog. Historical links establish
scope membership; moved or deleted items may remain diagnostic records without
becoming reachable notes.

Native rendering uses disposable scratch copied from a validated snapshot. Fresh engine options keep writes in scratch; copied `OPTIONS` files are not loaded. Byte-copy fallback is limited to 64 MiB per extraction. The RocksDB runtime is optional for the CLI, but required by the default native image layer; `GOODNOTES_ROCKSDB_LIBRARY` and `GOODNOTES_SCRATCH_DIR` select runtime and scratch paths.

Native page decoding reserves decompressed bytes per framed block before expansion, including work
in records that later fail to decode. Per-object and cumulative page limits remain distinct.
PDF fallback revalidates attachment confinement at each read and invalidates changed PDF handles
while retaining the session LRU. Completed snapshots must remain immutable; these checks do not
promise protection against every concurrent replacement of a path or its ancestors.

Observed support includes pen/highlighter records, filled paths, one ellipse form, variable-width pencil, one open-line form, and some linked PNG/JPEG images. Pencil texture is approximate. Unknown visibility, geometry, transforms, and records stay unknown or reduce coverage. A successful lookup with no page records reports `native-no-records-observed` / `no-native-records-observed` and warns that absence is unverified. Missing stores, runtime failures, and record-read errors remain errors. Generated tests do not prove Goodnotes compatibility or full rendering fidelity.

Public page numbers are one-based. Keep unknown/unavailable distinct from absent, false, or zero; preserve source labels, warnings, coverage, and annotation status. Annotated-PDF registration checks path, digest, and page count, but not page order or annotation inclusion; it adds no text ingestion.

Use `uv run goodnotes ...`. Command help and `goodnotes schema` define the public flags and output contract.
