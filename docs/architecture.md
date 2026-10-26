# Architecture

Goodnotes CLI reads a validated copy of a Goodnotes Library. `sync` creates or refreshes the copy. Browse and export commands read it. The CLI never opens a source SQLite database, controls Goodnotes, or writes to the source Library.

```text
Goodnotes Library -> inventory -> independent staging copy -> validate
                                               |                    |
                                               | failure            | marker written last
                                               v                    v
                                   keep previous pointer      active generation
                                                                    ^
                                  shared .view.lock: read <---------+
                                  exclusive .view.lock: publish/prune
```

## Refresh and read lifecycle

| Setting or command | Contract |
| --- | --- |
| Default cache | `~/.cache/goodnotes-cli`, based on `XDG_CACHE_HOME`. |
| Select cache | `GOODNOTES_DATA_DIR` or global `--data-dir`. |
| `--sandbox` | Use an existing marked copy, usually for development. |
| `snapshot LIBRARY DESTINATION` | Create a caller-owned copy. Keep it outside the managed cache; `sync` prunes only managed generations. |
| `watch` | Poll metadata and call the same refresh function after changes settle. Read commands do not refresh. |

### Capture invariants

| Phase | Invariant |
| --- | --- |
| Inventory | Compare source and active-copy metadata for databases, SQLite sidecars, optional native `notes_main.db` files, and attachments. Matching inventories allow reuse without hashing all bytes. `--force` bypasses reuse. |
| Copy | Copy changed files to independent inodes. macOS tries APFS clones; fallback copies bytes. Snapshot files are never hardlinked to source files. |
| Reuse | Reuse a prior verified digest only when source and cache metadata match. SQLite reuse requires the complete original database-and-sidecar family to match. |
| SQLite validation | Consolidate sidecars only in unpublished staging copies. Validate the copied projection database, then write the snapshot marker last. Keep source and completed-copy fingerprints separate. |
| Retry | Retry a detected source change up to three times. If no stable copy succeeds, fail and preserve the previous active pointer. |
| Publish | Compare the pre-capture and final source inventories. On mismatch, publish the validated view with `source_changed_during_refresh: true` and its `captured_at` time. The next sync captures again. |

Matching inventories mean only that the two checks matched. They do not prove the source stayed unchanged through command return. An edit after the final check is picked up by the next sync.

If a WAL changes while the source main database's full stat signature stays the same, capture may reuse that file's saved digest. It still copies and checks the current main file. A changed SQLite family never reuses the prior consolidated database.

Projection validation can be reused only when the SQLite family is unchanged and the marker version and copied-file digest match.

With `--verbose`, `capture_stats` reports file counts, source bytes hashed, clone and copy counts, and capture time. Byte counts are logical file sizes. Counters describe the successful attempt; elapsed time includes failed attempts.

## File access and locks

| Path | Writer | Reader | Rule |
| --- | --- | --- | --- |
| Source Library | None | `sync` copies files | Never open source SQLite or RocksDB. The Goodnotes app does not coordinate with CLI locks. |
| Staging generation | `sync` and SQLite consolidation | None | Independent, unpublished files only. Remove abandoned staging on the next sync. |
| Completed snapshot | None | CLI, read-only | Immutable after marker publication. |
| `active.json` and pruning | `sync` | Readers resolve pointer under lock | Update pointer and prune under exclusive `.view.lock`. |
| `.sync.lock` | `sync` | Other refreshes wait | Serialize the full refresh. |
| `.view.lock` | `sync` creates persistent file | Readers open read-only, shared | Hold through final database or attachment read. Sync waits for readers before publish/prune. |
| Artifact store | Render/bundle commands | Returned paths | Separate managed store, outside immutable snapshots. Explicit outputs remain caller-owned. |

Locks must be regular files, opened without following symlinks, and retain the same identity during acquisition. Readers do not create cache paths or recreate missing locks. Do not remove or replace a lock while a CLI process may hold it. A long reader delays publication and cleanup, while capture proceeds.

## SQLite copy consolidation

SQLite may write only to independent staging copies, after capture verification. The completed snapshot stays immutable and readers open it read-only.

| Copied family | Consolidation |
| --- | --- |
| WAL, no rollback journal | Make staging files writable; remove copied SHM so SQLite can rebuild it; checkpoint committed WAL pages; switch to DELETE journal mode; verify checkpoint; remove only empty sidecars after closing the connection. |
| Hot rollback journal | Recover the staging copy, then back it up into a separate staging database. |
| Non-WAL fallback | Write to staging output in DELETE journal mode. |

SQLite may accept only a valid committed WAL prefix. A successful checkpoint,
backup, or integrity check cannot prove that a torn or reset WAL tail held no
additional edits. Generated [snapshot tests](../tests/test_snapshot.py) cover both cases.

## Generated artifact storage

Without `--output`, `render` and `bundle` use the `artifacts/` store under the selected data directory. It is separate from snapshot generations. Artifact locking follows the shared view lock. Text-only readers do not initialize this store.

| Limit | Behavior |
| --- | --- |
| 100,000,000 logical bytes | Count unique PNG inodes, manifests, and entry metadata. Exclude small control files and in-progress staging. |
| 86,400-second idle expiry | Measure from the last CLI request. Capacity pressure may evict older unused entries earlier. |
| 5-minute lease | Protect a returned entry from expiry and capacity eviction. If protected entries block publication, return a capacity error. |
| Cleanup | Run on later artifact operations. There is no background process. |

| Cache key or publication | Included checks |
| --- | --- |
| Page render | Snapshot identity, current background/native fingerprints, page metadata, layer, DPI, and renderer version. |
| Native image | Copied attachment bindings and file signatures for the page's document. |
| Reuse | Check inputs before and after reuse. A cache hit does not open RocksDB. A renderer version change invalidates old page renders. |
| Annotated PDF | Verify the full digest once per operation. |
| Bundle | Read current text on every request. Key by text, render metadata, and provenance. |
| Publish and cleanup | Stage files and use ownership markers. Cleanup validates managed paths and uses no-follow removal. It never discovers caller output directories. |

New snapshot generations render again. Content-addressed PNG data can deduplicate identical output across generations. Generated PNGs may use hardlinks within the artifact store; source and snapshot files never do.

## Package boundaries

Keep changes inside the package that owns the behavior. `models` holds shared data structures; `errors` defines expected CLI failures.

| Package | Responsibility |
| --- | --- |
| `safety` | Validate copies and markers, reject live Library roots, enforce containment, and provide no-follow cleanup. |
| `snapshot` | Copy databases and attachments, verify changes, consolidate copied sidecars, validate projection, and mark completed snapshots. |
| `cache` | Manage active generations, pointers, refresh, locks, and pruning. |
| `library` | Own read-only connections and map folders, notebooks, pages, PDF bindings, stored text, and search results. |
| `text_index` | Batch read-only queries for recognition counts, PDF bindings, raw indexed rows, and diagnostics. `library` owns connections and result mapping. |
| `documents` | Resolve PDF backgrounds, render PNGs, export PDFs, verify reuse inputs, and publish bundles. Render/export default to `background`; study bundles default to `native`. |
| `annotations` | Decode observed protobuf, Apple LZ4, and TPL formats with bounded expansion. |
| `native`, `native_assets`, `native_render` | Open copied stores on disposable scratch, validate copied image bindings and rasters, draw supported records, and report coverage. |
| `artifacts` | Retain regenerable outputs, deduplicate payloads, enforce leases, and clean managed paths. It does not read Goodnotes databases or determine fidelity. |
| `watch`, `diagnostics` | Poll source metadata and report access/runtime availability without changing permissions. |
| `cli` | Define commands, validate options, and connect library and document operations. |
| `output` | Format structured responses, concise profiles, full metadata, and expected errors. |

Each `Library` session caches immutable schema metadata, hydrated pages, page references, and verified PDF bindings for one pinned view. PDF fallback extraction reuses a bounded set of open documents and closes them at operation end. These caches do not cross commands or snapshots.

Goodnotes search may require a custom FTS tokenizer. The CLI reads ordinary stored rows instead of virtual FTS tables that require that tokenizer.

## Native rendering and annotations

Native rendering opens RocksDB only on disposable scratch cloned from a validated copy. Fresh engine options keep writes in scratch; copied `OPTIONS` files are not loaded. Remove scratch after extraction. A 64 MiB limit applies to byte-copy fallback per extraction.

| Observed support | Limits |
| --- | --- |
| Pen and highlighter records; filled paths; one outlined ellipse form; variable-width pencil strokes; one two-point open stroked shape; some linked PNG/JPEG images. | Pencil texture is approximate. Images require exact copied attachment binding, matching placement rectangles, and unrotated, uncropped placement. |
| Decoded native records | Unknown visibility, transforms, versions, active types, or decode failures reduce coverage. Fail if no supported object remains. |
| Missing native root | Report unavailable data, not an empty page. Never silently substitute background-only output. |

Generated checks cover observed records. They do not establish compatibility with every Goodnotes format or full visual fidelity. See [verification](verification.md) for the evidence boundary and [PDF and handwriting](pdf-and-handwriting.md) for output interpretation.

## Data and numbering

| Contract | Value |
| --- | --- |
| Page numbers | Public pages are one-based, including `--page` and both ends of `--pages`. Convert to zero-based PDF indexes at the CLI or document boundary. |
| JSON | Compact by default. `--text` indents JSON; `--verbose` returns full metadata; `--fields` projects the full contract. |
| Unknown values | Keep unknown/unavailable distinct from absent, false, or zero. Preserve source labels, warnings, coverage, and annotation status. |
| List/tree | 100 items by default. |
| Search | 20 snippets by default. |
| Read | Page 1 by default; at most 20 pages and 12,000 combined characters by default. |
| Bundle | At most 10 pages and one shared 12,000-character default text budget. |
| Pagination | Results include generation ID `snapshot`. `--expect-snapshot ID` rejects a sequence after refresh; restart against the new view. |
| Annotated PDF | Registration checks path, digest, and page count. It does not verify order or annotation inclusion, copy the PDF, or ingest text. Status is `user-provided-unverified`. |

## Dependencies and execution

| Requirement | Details |
| --- | --- |
| Python | 3.11 or newer. `uv` manages the project environment. |
| CLI and PDF | Typer provides command parsing; PyMuPDF reads and renders PDFs. SQLite uses the standard library. |
| Native engine | Compatible RocksDB shared library and writable scratch. On macOS, install with `brew install rocksdb`. |
| Runtime paths | `GOODNOTES_ROCKSDB_LIBRARY` selects an absolute shared-library path; `GOODNOTES_SCRATCH_DIR` selects scratch. |
| Diagnostics | `doctor --access-only` reports access and optional runtime availability without creating files or opening databases. |

Use `uv run goodnotes ...`; do not install project dependencies globally. Put global options such as `--data-dir`, `--sandbox`, `--fields`, `--text`, `--json`, and `--expect-snapshot` before the command name.
