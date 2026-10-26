# Images and handwriting

## Choose a layer

| Layer | Contents | Requirement or limit |
| --- | --- | --- |
| `background` | Copied PDF/image background | No native ink overlay. Existing flattened marks remain visible. |
| `native` | Background plus supported reconstructed objects | Optional RocksDB runtime and writable scratch; approximate, experimental fidelity. |
| `annotated` | Your registered Goodnotes PDF export | Full-notebook export; annotation inclusion and page order remain unverified. |

Render/export default to **background**. Bundle defaults to **native images and PDF text**.
Bundle's `--source` changes text only. The CLI runs no image OCR and never silently
substitutes a background for failed native or annotated output.

## Native setup

The Python installation suffices for browsing, text, and background output.
Native reconstruction also needs a compatible RocksDB shared library. On macOS:

```sh
brew install rocksdb
uv run --locked goodnotes doctor --access-only
uv run --locked goodnotes sync
uv run --locked goodnotes bundle "Course Notes" --pages 3
```

Use `sync --library /path/to/Library` for an explicit source. Sync captures the native
store when present; older copies without it need a new capture. The CLI discovers
standard Homebrew paths and the system library path. `GOODNOTES_ROCKSDB_LIBRARY`
can select an absolute shared-library path. A missing/incompatible library fails explicitly.

Native reads use disposable scratch only. `GOODNOTES_SCRATCH_DIR` can select an
existing private writable directory outside the snapshot; otherwise system temporary
storage is used. Without APFS cloning, the native store copy budget is 64 MiB per extraction.

Observed support covers legacy pen/highlighter records, filled paths, one ellipse
form, one two-point open shape, variable-width pencil strokes, and some PNG/JPEG
images with exact untransformed attachment bindings. Pencil texture is approximate.
Unsupported geometry, transforms, object versions, or missing native data can fail
or reduce coverage. See [native implementation](../src/goodnotes_cli/native_render.py)
and [failure behavior](robustness.md) for technical limits.

Always inspect coverage, unsupported reasons, and warnings. Zero unsupported objects
does not establish completeness; `full_fidelity_verified` remains false. PNG scanlines
are checked; JPEG scan integrity is decoder-unverified. Missing image body field 6
keeps its appearance/visibility warning. See [agent interpretation](agent-guide.md#interpret-the-evidence).

## Stored recognition

```sh
uv run --locked goodnotes --fields number,recognition pages "Course Notes" --with-recognition
uv run --locked goodnotes read "Course Notes" --page 3 --source handwriting
```

`stored-text` means candidates exist; `no-stored-text` describes only this copy's
index, not visible ink. `unavailable` means the source could not be queried, so
counts are unknown. Index state is `pending`, `not-pending`, or `unknown`; none
verifies accuracy or freshness. Candidate text preserves alternatives and delimiters.
Quote exact raw text; label a cleaned reading as interpretation.

## Use a PDF exported by Goodnotes

Use the app's own rendering for elements native reconstruction cannot supply.
This route needs no RocksDB runtime.

1. Export the **whole notebook** with **Include Annotation** and **Include Page Background** enabled.
   See [Goodnotes' instructions](https://support.goodnotes.com/hc/en-us/articles/360000629576-Include-Annotations-and-Page-Backgrounds-in-PDF-Exports).
2. Check its page order against the current notebook yourself.
3. Register and use the PDF:

   ```sh
   uv run --locked goodnotes attach-pdf "Course Notes" /path/to/notebook-export.pdf
   uv run --locked goodnotes render "Course Notes" --page 3 --layer annotated --output /tmp/page-3-annotated.png
   ```

Use `export --layer annotated --output NEW_PDF` for PDF output. Registration checks
readability, page count, and content integrity before reuse. It stores the path in
sidecar storage without copying the PDF. Keep it in place and unchanged; register
again after it moves/changes or sync publishes a new generation.

Output is `user-provided-unverified` with `includes_handwriting: null`. Count and
signature checks cannot verify annotation inclusion or page order. Registration
adds no text to read/search/PDF scans.
