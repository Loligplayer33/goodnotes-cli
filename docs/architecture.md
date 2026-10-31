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

Write authorization protects the selected snapshot and the physical source path recorded in its
marker, including a recorded location that is now missing. Explicit outputs, artifact storage and
annotated-PDF sidecar directories must be disjoint from these known roots in both directions.
Sandbox parses source provenance only for writes. An absent historical `source_path` leaves the
source unknown; a present malformed value blocks writes without blocking navigation or text reads.
Virtual catalog root selection does not change physical source protection.

Containment uses resolved paths and current filesystem identity. It does not rediscover a source
moved away from its recorded name or unknown source locations in imported snapshots. Admission and
filesystem acquisition remain separate operations, so they do not prevent every concurrent ancestor
replacement. Captured provenance adds deny roots and never grants access to source data.

## Artifact cache

Managed `render` and `bundle` outputs use a separate artifact store; explicit output paths remain caller-owned. Cleanup runs during later artifact operations, not in a background process.
Production callers pass the selected Sandbox to ArtifactCache so admission includes its source
provenance before storage creation or maintenance and again after the root is opened. The legacy
bare snapshot-path constructor protects only the snapshot, with unknown source provenance and no
implicit marker or database reads.

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
Page-number validation follows the resolved notebook scope before inspecting stored identities.
Stored text IDs and PDF page bindings use exact integer checks. Bounded text queries retain
unmatched candidate metadata so invalid reached rows cannot disappear as empty results; rows beyond
the requested text budget remain unexamined. Search owns each source producer within its notebook
batch, and each producer closes its cursor before the borrowed session connection is released.

ArtifactCache enters its private filesystem owner once and keeps all borrowed descriptors inside the
session. Stages, pins and publication state belong to the transaction owner. Retention accounting
passes known directory identities through deletion. Native scratch encloses every engine and iterator handle.

Artifact copy and blob-scan scopes close acquired local descriptors even when initial validation is
interrupted. Root preparation failure and session release detach stored ownership before close;
provisional lock cleanup preserves acquisition errors, and release attempts unlock and both closes
once. These cleanup paths retain an earlier operation error; a cleanup-only failure remains an error.
A failed or interrupted close is not retried.
Orphan cleanup rechecks each inventoried identity and its current file/link metadata before unlinking
through the pinned blobs directory. New names wait for a later pass. The final stat and unlink remain
separate, non-atomic calls against arbitrary same-user replacement.

Snapshot markers contain the captured file inventory and are limited to 64 MiB. If the generated
marker exceeds this limit, capture fails before publication and removes its staging copy; the
previous active snapshot remains available.

Capture retains one owner for its created root, each attempt's `Library`, and marker files.
It pins the parent and root descriptors through managed generation and active-pointer publication.
Failure cleanup checks those retained identities, removes known children relative to the pinned
root, and finishes with `rmdir`. It does not recursively delete the root or adopt replacement
entries found after failure. Uncertain interrupted publication can leave an unreferenced tree.
The existing later abandoned-staging cleaner still authorizes the reserved `.staging-<uuid>`
namespace; a replacement preserved during one call can be removed by a later refresh. Capture
does not maintain a persistent identity journal or a creation ledger for every nested file.
Identity checks reject observed replacements but do not make separate stat/unlink/rmdir calls
atomic against arbitrary same-user mutation.

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
One rendering context owns the explicit bundle stage from creation through rendering and
publication. The CLI borrows its path; publication borrows its pinned descriptors and records
completion before closing its own destination descriptor. Cleanup retains that completion fact
rather than inferring success from the current destination pathname. Every acquired descriptor
gets one close attempt. An earlier operation error remains primary; a cleanup-only error is raised
after the remaining closes, and does not undo completed output or remove its parent.

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
Scratch parents must stay outside the snapshot and known source roots. A common ancestor may host
a fresh sibling scratch directory. The actual child is checked for disjointness before copying
bytes or opening the engine, within its cleanup scope. This check follows allocation of an empty
unique directory and does not promise zero transient allocation at a missing recorded source path.

Native page decoding reserves decompressed bytes per framed block before expansion, including work
in records that later fail to decode. Per-object and cumulative page limits remain distinct.
The generic TPL parser also shares a limit of 1,000,000 logical work units across one image.
Units count retained schema nodes, decoded values and their list/tuple containers, including root
and empty-group tuples. Fixed-array decoding reserves the same cost before bulk expansion.
This bounds retained logical expansion and proportional decoding work, not exact heap bytes or
every temporary allocation. Existing byte, array and nesting limits remain separate. Native callers
check expected formats before generic parsing; arbitrary-format CLI reachability is unproven.
Copied-store inspection and scratch preparation translate filesystem failures at their native
operation boundary, preserving causes. Scratch preparation ends before the caller body, so a
caller's filesystem error is not relabeled as a copying failure.
PDF fallback revalidates attachment confinement at each read and invalidates changed PDF handles
while retaining the session LRU. Completed snapshots must remain immutable; these checks do not
promise protection against every concurrent replacement of a path or its ancestors.

Native envelopes compile into shared move/line/quadratic/cubic drawing records. Ink commands retain disconnected runs; filled arrays retain subpath boundaries. Circular and rotated elliptical arcs compile directly to cubic controls. Shapes share geometry, style and translation decoding; images preserve local size and rotation while validating their stored bounding boxes.

| Owner | Implementation |
| --- | --- |
| `native.records`, `native.values` | Shared drawing records, structured decode issues, bounded wire/TPL helpers and scalar policies. |
| `native.ink`, `native.paths`, `native.geometry` | Ink commands, filled path arrays, patterns, shapes, fills and image transforms. |
| `native.objects` | Pairing, identity, ordering, partial observations and coverage. |
| `native.assets`, `native.raster` | Copied resource binding, stable reads and raster validation. |
| `rendering.ink_svg`, `rendering.svg` | Stroke sampling, validated SVG primitives and bounded pattern composition. |
| `rendering.overlay` | Page mapping, resource resolution and overlay error translation. |

Decoders use the shared drawing records without importing rendering. Rendering uses those records; overlay combines decoding, copied resources and drawing. Generated wire/TPL builders remain independent of production format constants in `tests/support/native_oracle.py`.

Observed support includes variable-width pencils and scale-1 patterned fills. Pencil decoding retains the configured width and relative sample dynamics. Rendered tone, stroke width, taper, contours, absolute pressure response and texture remain approximate. Faint gray details around some fill edges remain unverified. Patterns use the observed six raster pixels per native unit and brush anchoring; other resources' resolution remains unverified. Bounded raster masks implement pattern clipping because the SVG backend does not implement pattern paints or clip paths. Unknown visibility, geometry, transforms, and records stay unknown or reduce coverage. A successful lookup with no page records reports `native-no-records-observed` / `no-native-records-observed` and warns that absence is unverified. Missing stores, runtime failures, and record-read errors remain errors. Generated tests do not prove Goodnotes compatibility or full rendering fidelity.

Public page numbers are one-based. Keep unknown/unavailable distinct from absent, false, or zero; preserve source labels, warnings, coverage, and annotation status. Annotated-PDF registration checks path, digest, and page count, but not page order or annotation inclusion; it adds no text ingestion.

Use `uv run goodnotes ...`. Command help and `goodnotes schema` define the public flags and output contract.
