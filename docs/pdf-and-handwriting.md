# Images, PDFs, and handwriting

Choose the image layer for your task. Text selection is separate from image layers.
The CLI does not run OCR on images.

## Choose a layer

| Layer | Image contents | Requirements | Limit |
| --- | --- | --- | --- |
| `background` | Copied PDF or image background | Normal install and synced copy | Does not overlay native Goodnotes ink. Marks already flattened into a PDF remain visible. |
| `native` | Background plus supported reconstructed native objects | Copied native store, compatible RocksDB runtime, writable scratch | Experimental and approximate. Full fidelity is unverified. |
| `annotated` | Pages from your registered Goodnotes PDF export | A full-notebook PDF registered with `attach-pdf` | The CLI cannot verify annotation inclusion or page order. |

`render` and `export` default to **background**. `bundle` defaults to **native**
images and **pdf** text. Its `--source` option changes text only.

## Native ink without manual PDF exports

The Python installation alone is enough for browsing, text, and backgrounds.
For native reconstruction, install a compatible RocksDB shared library separately.
On macOS with Homebrew:

```sh
brew install rocksdb
uv run --locked goodnotes doctor --access-only
```

The CLI checks standard Apple Silicon and Intel Homebrew paths, then the system
library search path. For a custom installation, set `GOODNOTES_ROCKSDB_LIBRARY`
to the absolute shared-library path. The CLI does not install it automatically.
`doctor --access-only` reports runtime availability and filesystem access;
write checks are hints and perform no writes.

Refresh the copy, then request native output:

```sh
uv run --locked goodnotes sync
uv run --locked goodnotes bundle "Course Notes" --pages 3
uv run --locked goodnotes render "Course Notes" --page 3 --layer native --output /tmp/page-3-native.png
uv run --locked goodnotes export "Course Notes" --pages 3 --layer native --output /tmp/page-3-native.pdf
```

Use `sync --library /path/to/Library` for an explicit source. Sync captures
`notes_main.db` when present. Older copies without that store need a new capture.
Native rendering opens only disposable scratch copies. Scratch defaults to the
system temporary directory; `GOODNOTES_SCRATCH_DIR` can select an existing private
writable directory outside the snapshot. Without APFS cloning, the native reader
allows independent copying only within a 64 MiB store-copy budget.

### What native reconstruction supports

| Object or detail | Current behavior |
| --- | --- |
| Observed legacy pen/highlighter records, RGBA colours, supported lasso translations | Reconstructed for supported records |
| Filled paths, one outlined ellipse form, an observed two-point open shape, one variable-width pencil format | Supported within the tested layouts; pencil texture remains approximate |
| Linked PNG/JPEG images | Require exact copied binding, matching top/center rectangles, no rotation or crop |
| Other shapes, text boxes, complex erasure, ambiguous versions, cross-actor drawing order | Unsupported or uncertain |
| Unsupported page/background rotations, coordinate scales, or PDF crop geometry | Explicit failure |
| Missing native root | Unknown annotation availability, not proof of a blank page |

Inspect `native_annotations.coverage`, object counts, unsupported reasons, and
warnings. `full_fidelity_verified` remains `false`, even with zero unsupported objects.
`rendered_strokes` includes supported pen marks and filled paths;
`rendered_paths` and `rendered_images` are separate counts. Images do not establish
that handwriting was rendered.

PNG scanlines are checked. JPEG scan integrity remains decoder-unverified and
keeps a warning. Missing native image body field 6 also retains a warning about
unverified appearance or visibility. Neither case establishes full coverage.
For interpretation rules, see the [agent guide](agent-guide.md#interpret-page-evidence).

## Export page backgrounds

No native runtime is needed:

```sh
uv run --locked goodnotes render "Course Notes" --page 3 --output /tmp/page-3-clean.png
uv run --locked goodnotes export "Course Notes" --pages 1-3,7 --output /tmp/background-pages.pdf
uv run --locked goodnotes bundle "Course Notes" --pages 3 --layer background
```

Use `--layer native` only when you want reconstruction. A failed native or
annotated request never silently becomes a clean background image.

## Find stored handwriting recognition

```sh
uv run --locked goodnotes --fields number,recognition pages "Course Notes" --with-recognition
uv run --locked goodnotes read "Course Notes" --page 3 --source handwriting
```

| Recognition value | Meaning |
| --- | --- |
| `stored-text` | This copy has stored text for the source. |
| `no-stored-text` | No text is stored for the source in this copy. This says nothing about visible ink. |
| `unavailable` | The source could not be queried. Counts are unknown, not zero. |
| `pending` / `not-pending` / `unknown` | Stored indexing state. It does not verify freshness or accuracy. |

Page metadata does not return candidate text or inspect ink. `read` returns raw
stored candidates with their alternatives and delimiters. Label a cleaned reading
as interpretation. The CLI does not perform handwriting recognition itself.

## Use a PDF exported by Goodnotes

Use this route when you need the app's own rendering, including elements native
reconstruction does not support. You do not need RocksDB for this layer.

1. Export the **whole notebook** as a PDF in Goodnotes with **Include Annotation**
   and **Include Page Background** enabled. See [Goodnotes' export instructions](https://support.goodnotes.com/hc/en-us/articles/360000629576-Include-Annotations-and-Page-Backgrounds-in-PDF-Exports).
2. Check the PDF's page order against the current notebook yourself.
3. Register the PDF against your current synced copy:

   ```sh
   uv run --locked goodnotes attach-pdf "Course Notes" /path/to/notebook-export.pdf
   ```

4. Use the registered pages:

   ```sh
   uv run --locked goodnotes render "Course Notes" --page 3 --layer annotated --output /tmp/page-3-annotated.png
   uv run --locked goodnotes export "Course Notes" --pages 1-3 --layer annotated --output /tmp/annotated-pages.pdf
   ```

Registration checks readability and the notebook's current page count. It records
the PDF path without copying it, then checks content integrity before reuse.
Keep that file in place and unchanged. Register again after it moves or changes,
or when sync publishes a different generation.

Output is labelled `user-provided-unverified`; `includes_handwriting` is `null`.
Page-count and signature checks cannot establish annotation inclusion or correct
page order. Registration does not add the PDF's text to `read`, `search`, or `scan-pdfs`.
