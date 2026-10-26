# Troubleshooting

Start with the structured error's `code`, `message`, and `details` on stderr.
Access errors include the failing `path`, `operation`, and `remedy`.
The CLI does not change permissions or bypass validation.

## Setup and access

Check paths before loading notes or creating outputs:

```sh
uv run --locked goodnotes doctor --access-only
```

For a custom cache, put `--data-dir PATH` before `doctor`. Add `--library PATH`,
`--scratch-dir PATH`, or `--output-dir PATH` after `doctor --access-only` to inspect
those boundaries. Write-access checks create nothing and remain hints.

| Symptom | Action |
| --- | --- |
| `cache_not_initialized` | Run sync with a process allowed to read the source and write the selected cache. Use the same data directory when reading. |
| Missing managed view lock | Stop other CLI processes, then sync to repair it. Never delete or replace active lock files. |
| `cache_permission_denied` / `source_permission_denied` | Use `details.path` and `remedy` to adjust the terminal or agent runner's access. Do not silently choose another cache. |
| `cache_invalid` | Read the specific validation error. Stop other processes before repairing via sync; preserve caller-owned copies. |
| Sync waits with no output on macOS | Check for a pending system privacy prompt for the process reading Goodnotes. |
| `stale_snapshot` | Restart the multi-command sequence against the new generation. |

See [access and refresh](access.md) for the setup/reader permission boundary.

## Missing text or search results

| Symptom | Action |
| --- | --- |
| Search returns no hits or reports an unavailable index | Check coverage and warnings. For slide text, run scoped `scan-pdfs`; browsing still works without a search index. |
| Scan is incomplete | Follow `next_page_offset` as `--page-offset` with the same query, scope, and snapshot. Check extraction gaps. |
| Handwriting text is empty | Check recognition metadata and the page image. No stored candidates does not prove there is no ink. |
| Text stops early | Check `truncated`; request fewer pages or increase `--max-chars`. |
| Text from an attached PDF is missing | `attach-pdf` supplies the annotated image layer; it does not ingest text. |

Use the [search recipe](usage.md#search-notes-and-pdf-backgrounds) to choose a text source.

## Images and exports

| Symptom | Action |
| --- | --- |
| `native_runtime_unavailable` | Install a compatible RocksDB shared library or correct `GOODNOTES_ROCKSDB_LIBRARY`. Use the [native setup guide](pdf-and-handwriting.md#native-ink-without-manual-pdf-exports). |
| `native_data_unavailable` or partial native coverage | Inspect object counts and unsupported reasons. Refresh if the copy is old. Use an app-exported PDF when native reconstruction cannot supply the needed evidence. |
| Default bundle fails, but background render works | Bundle defaults to native. Use `--layer background` only when a clean image is what you need. |
| Handwriting is absent from render/export | Their default layer is background. Choose native reconstruction or a registered annotated PDF explicitly. |
| `status: "partial"` in a bundle | Inspect `failed_pages` and each `render_error`. Those rows have no image. |
| Annotated output fails after refresh or a PDF edit | Register the full matching PDF again against the current generation. |
| Returned image path disappeared | Repeat the request or supply `--output` for a persistent result. |
| `artifact_cache_capacity` | Retry after protected entries can expire, or use explicit output. |
| Destination already exists | Choose a new file or directory. The CLI does not overwrite outputs. |

For unresolved issues, [open a GitHub issue](https://github.com/Loligplayer33/goodnotes-cli/issues)
with the version, command, error code, and a synthetic reproduction. Redact note
text, notebook names/IDs, personal paths, and email addresses.
