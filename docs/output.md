# Results and output

Commands return compact JSON by default. Use global `--text` to indent it,
`--verbose` for full metadata, or `--fields` to select fields from the full contract.
Global options go before the command.

```sh
uv run --locked goodnotes --text read "Course Notes" --page 3
uv run --locked goodnotes --verbose render "Course Notes" --page 3
uv run --locked goodnotes --fields number,recognition pages "Course Notes" --with-recognition
```

## Read the evidence

| Field or signal | How to use it |
| --- | --- |
| `source` on text parts | Distinguish `pdf`, stored `handwriting` candidates, and typed `textbox` text. |
| `warnings` | Preserve limits when interpreting the result, including a result with no text or hits. |
| `truncated` | Some requested text did not fit the character budget. |
| `coverage` and `coverage_detail` | Identify what was searched or reconstructed. They do not prove complete handwriting coverage. |
| `annotation_status` | Distinguish backgrounds, approximate native reconstruction, and unverified app-exported PDFs. |
| `null` or unavailable data | Unknown is distinct from absent, false, and zero. |

`--fields` selects row keys for `items`, `pages`, or `hits`, keeping sibling warnings,
cursors, and snapshot metadata. For other responses, including bundle, it selects
top-level keys. It uses the full field contract rather than the concise profile.
An unknown field fails with the valid keys. `recognition` requires `pages --with-recognition`.
Use `schema COMMAND` for valid fields and output variants.

## Pagination and consistent reads

| Result | Continue with |
| --- | --- |
| `ls`, `tree`, `pages`, `search`: `next_offset` | `--offset` |
| `scan-pdfs`: `next_page_offset` | `--page-offset` |

Use the returned cursor value, preserving scope and other options. A scan cursor
counts pages scanned, not hits. For a consistent sequence, copy the first result's
`snapshot` into later commands:

```sh
uv run --locked goodnotes ls --limit 20
uv run --locked goodnotes --expect-snapshot SNAPSHOT_ID ls --limit 20 --offset NEXT_OFFSET
```

`SNAPSHOT_ID` and `NEXT_OFFSET` are placeholders for returned values.
On `stale_snapshot`, restart against the new generation. Do not combine results
from different generations or sync between matching reads.

One bundle holds one read view across its images and text. It needs no generation
guard unless you make later matching requests.

| Command result | Generation ID | Filesystem path |
| --- | --- | --- |
| `sync` / successful watch refresh | `snapshot_id` | `snapshot` in verbose output |
| Cache-backed reads | `snapshot` | Other path fields depend on command |
| Standalone `snapshot` | Not returned as a separate ID | `snapshot` is the destination path |

A new capture gets a new generation ID. An unchanged sync keeps the current one.
Document and page IDs remain stable across ordinary refreshes.

## Temporary and saved files

| Output choice | Lifetime |
| --- | --- |
| `render` or `bundle` without `--output` | Temporary managed artifact cache. Open promptly. |
| Explicit `--output` | Caller-owned persistent file or directory. Never automatically pruned. |
| `export` | Requires an explicit new output path. |

The artifact cache lives under `artifacts/` in the selected data directory.
It retains up to **100,000,000 logical bytes**, counting identical PNGs once.
Entries expire after **24 hours since their last CLI use**. Cleanup runs on the
next managed artifact operation, with a **five-minute protection period** after use.
After that period, capacity cleanup can remove a path before its 24-hour expiry.

If a path expired, repeat the request. That reads the currently selected copy,
not a historical generation. A cache that cannot fit a result without removing
protected entries returns `artifact_cache_capacity`; retry later or use `--output`.
Saved outputs must use a new destination outside the snapshot and source Library.

## Partial native bundles

A multi-page native bundle can succeed for some pages and fail for others:

| Signal | Meaning |
| --- | --- |
| Top-level `status: "partial"` and `failed_pages` | At least one image succeeded and a page-local native failure affected another. |
| Failed row: `image: null`, `render_error` | Text fields are retained and may be empty. There is no image evidence for that page. |
| Single-page or all-page image failure | Structured error; no bundle is published. |

Failed pages never become background-only images. Native-runtime, snapshot-integrity,
safety, and publication failures are fatal. Unavailable or invalid native image
objects may instead be omitted with partial-coverage warnings. Keep successful-page warnings
as well. The on-disk manifest retains full diagnostics in every output mode.

## Errors and exit status

| Exit | Meaning |
| --- | --- |
| `0` | Command completed. Still inspect coverage, warnings, and partial status. |
| `1` | Expected operation error, including application-checked values such as an invalid page expression. |
| `2` | Invalid command syntax or parser-checked option bounds. |
| `130` | Interruption or a closed output pipe. |

Errors are complete JSON on **stderr**, with `error.code`, `message`, and optional
`details`. Access errors include `operation`, `path`, and `remedy`.
Help and version are plain text. `watch` emits a sequence of JSON events on stdout,
including retry errors, rather than one result. Explicit `--json` takes precedence
if combined with `--text`.
