# Images and handwriting

## Choose a layer

| Layer | Contents | Requirement or limit |
| --- | --- | --- |
| `native` | Copied PDF/image background plus supported reconstructed objects | Default for `bundle`, `render`, and `export`. Requires the optional RocksDB runtime and writable scratch; fidelity is approximate. |
| `background` | Copied PDF/image background | Explicit ink-free layer. Any marks already flattened into the background remain visible. |
| `annotated` | Registered Goodnotes PDF export | Uses the app-rendered page. Annotation inclusion and page order cannot be verified. |

Native output reports coverage and warnings; it does not establish full fidelity.
Missing native data and render errors remain visible, with no silent background
substitution. Use `--layer background` when a clean background is needed or native
rendering is unavailable.

If a native lookup succeeds but finds no page records, the result reports
`annotation_status: native-no-records-observed` and
`coverage: no-native-records-observed`, with an unverified-absence warning. The
background image is available, but the result does not prove that the page has no
ink. Missing stores, runtime failures, and read errors remain errors.

## Native setup

The Python installation suffices for browsing, text, and background output. The
default native image layer also needs a compatible RocksDB shared library. On
macOS:

```sh
brew install rocksdb
uv run --locked goodnotes doctor --access-only
uv run --locked goodnotes sync
uv run --locked goodnotes bundle "Course Notes" --pages 3
```

Use `sync --library /path/to/Library` for an explicit source. Sync captures the
native store when present; older copies without it need a new capture. The CLI
discovers standard Homebrew paths and the system library path.
`GOODNOTES_ROCKSDB_LIBRARY` can select an absolute shared-library path. A
missing or incompatible library fails explicitly.

Native reads use disposable scratch. `GOODNOTES_SCRATCH_DIR` can select an
existing private writable directory outside the snapshot; otherwise system
temporary storage is used. Without APFS cloning, the native-store copy budget is
64 MiB per extraction.

Observed support covers legacy pen/highlighter records, filled paths, one ellipse
form, one two-point open shape, variable-width pencil strokes, and some PNG/JPEG
images with exact untransformed attachment bindings. Pencil texture is approximate.
Unsupported geometry, transforms, object versions, or missing native data can
fail or reduce coverage. Zero unsupported objects does not establish completeness;
`full_fidelity_verified` remains false. PNG scanlines are checked; JPEG scan
integrity is decoder-unverified. Missing image body field 6 keeps its warning.
See [failure behavior](robustness.md).

## Text sources and stored recognition

`read`, `search`, and `bundle` default to `--source text`, which includes indexed
PDF text and typed textboxes. Use `--source all` to add stored handwriting
recognition candidates, or `--source handwriting` to select candidates alone.
The CLI reads the existing index; it does not run OCR on images or native ink.

Recognition metadata describes the stored index, not the visible page. `stored-text`
means candidates exist; `no-stored-text` applies only to this copy's index, not to
visible ink. `unavailable` means the source could not be queried, so counts are
unknown. Index state is `pending`, `not-pending`, or `unknown`; none verifies
accuracy or freshness. Candidate text preserves alternatives and delimiters.
Quote exact raw text; label a cleaned reading as interpretation.

```sh
uv run --locked goodnotes --text pages "Course Notes"
uv run --locked goodnotes read "Course Notes" --pages 3 --source handwriting
```

## Use a Goodnotes-exported PDF

Use the app-rendered page for elements native reconstruction cannot supply. This
advanced path needs no RocksDB runtime.

1. Export the **whole notebook** with **Include Annotation** and **Include Page Background** enabled. See [Goodnotes' instructions](https://support.goodnotes.com/hc/en-us/articles/360000629576-Include-Annotations-and-Page-Backgrounds-in-PDF-Exports).
2. Check its page order against the current notebook; the CLI cannot verify that mapping.
3. Register and use the PDF:

   ```sh
   uv run --locked goodnotes attach-pdf "Course Notes" /path/to/notebook-export.pdf
   uv run --locked goodnotes bundle "Course Notes" --pages 3 --layer annotated
   ```

Registration checks readability, page count, and content integrity. It stores the
PDF path in sidecar storage without copying the file. Keep the PDF unchanged at
that path; register again if it moves, changes, or a new snapshot is published.
The output is `user-provided-unverified` with `includes_handwriting: null`.
Registration does not verify annotation inclusion or page order and does not add
PDF text to `read`, `search`, or PDF scans.
