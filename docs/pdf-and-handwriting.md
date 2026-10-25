# PDFs and handwriting

The CLI offers three image layers. `background` uses the page's stored PDF or image. `native` reconstructs supported annotations from the copied Goodnotes store, with partial coverage and fidelity warnings. `annotated` uses a PDF that you exported from Goodnotes and registered with the CLI. None of these layers runs OCR on images.

## Native ink without manual PDF exports

Install the Python project environment from the repository root with `uv sync`. Native rendering also needs a
compatible RocksDB shared library, installed separately from the Python dependencies. On macOS with Homebrew:

```sh
brew install rocksdb
uv run goodnotes doctor --access-only
```

The CLI discovers the standard Apple Silicon and Intel Homebrew locations, then the system library search path. For a custom installation, set `GOODNOTES_ROCKSDB_LIBRARY` to its absolute shared-library path. The runtime must support the format of the copied store; an incompatible or missing library produces `native_runtime_unavailable` rather than a background-only image. The CLI does not install system libraries automatically.

Create a new copy with `sync`, or start `watch` and keep it running while you study:

```sh
uv run goodnotes watch
```

In another terminal, render or bundle the page you want an agent to inspect:

```sh
uv run goodnotes render "Notebook name" --page 3 --layer native --output /tmp/page-3-native.png
uv run goodnotes bundle "Notebook name" --pages 3 --output /tmp/page-3-study
uv run goodnotes export "Notebook name" --pages 3 --layer native --output /tmp/page-3-native.pdf
```

These commands do not require a manually exported PDF. Sync includes the native `notes_main.db` store when available. A copy made before native capture was added needs a new sync. Watch sees edits after Goodnotes has written them on this Mac; it cannot establish whether the latest iPad edit has arrived. Check the returned `snapshot`, `captured_at`, and warnings before judging a new note.

Native results report `annotation_status: "native-reconstructed-approximate"` and a `native_annotations` object. Its `coverage` is `supported-pen-records`, `supported-native-records`, or `partial`, with rendered, erased, deleted, and unsupported object counts and reasons. `rendered_strokes` includes supported pen marks and filled paths. `includes_handwriting: true` means at least one of those was rendered; native images do not count as handwriting. The concise response includes the separate `rendered_paths` and `rendered_images` counts, so an agent can identify image-only pages. A zero `unsupported_objects` count does not prove completeness; opaque fields and unverified drawing order can still produce partial coverage. `--verbose` adds the full native metadata. `full_fidelity_verified` remains `false`.

Supported reconstruction covers observed legacy TPL pen and highlighter records, RGBA colours, supported lasso translations, filled paths, one outlined ellipse form, and one variable-width pencil format. Pencil texture is approximate. Native PNG and JPEG images are loaded only when their copied attachment binding is exact, their stored top and center rectangles match, and the image is unrotated and uncropped. Other shapes and text boxes, complex erasure, ambiguous object versions, and drawing order across multiple actors remain unsupported or uncertain. Unknown active objects and unmodeled fields produce partial-coverage warnings. No supported objects, missing native records, and unsupported page geometry fail explicitly; an absent native root is unknown annotation availability, not evidence of a blank page. The projection-to-native page mapping is experimental. Page/background rotations, nonmatching coordinate scales, image transforms, and unsupported PDF crop geometry are rejected.

Image validation rejects malformed PNG scanlines and incomplete JPEG structures. JPEG scan integrity remains unverified because the decoder can repair damaged data. Native results retain that warning and partial coverage; verbose `image_integrity` counts distinguish `scanlines-checked` from `decoder-unverified` images.

The native engine opens only a disposable private scratch copy of the validated snapshot. It never opens the source store or the completed snapshot store. Scratch defaults to the operating system temporary directory; set `GOODNOTES_SCRATCH_DIR` to an existing private writable directory outside the snapshot when necessary. APFS clones avoid another full byte copy. Without clone support, the reader allows independent copying only for stores up to 64 MiB and rejects larger stores. Native rendering therefore needs cache read access, scratch write access, and output write access. Use `doctor --access-only --scratch-dir PATH --output-dir PATH` to inspect those access boundaries without changing them.

## Export page backgrounds

Standalone render/export default to `background`, using the PDF or image attachment assigned to each page. Bundle defaults to native ink; use `bundle --layer background` for clean page images:

```sh
uv run goodnotes export "Notebook name" --output /tmp/notebook-backgrounds.pdf
uv run goodnotes render "Notebook name" --page 3 --output /tmp/page-3.png
```

The CLI keeps the notebook's current page order. Background output does not overlay Goodnotes handwriting, highlights, or other native annotations. Marks already flattened into a source PDF remain part of that background. The CLI does not run OCR on image backgrounds.

Use `export --pages 1-3,7` to export selected pages in the listed order. Without `--pages`, export includes every page.

## Find stored handwriting recognition

`pages --with-recognition` adds status metadata to the normal `pages` array. It does not return candidate text or inspect the page image:

```sh
uv run goodnotes --fields number,recognition pages "Notebook name" --with-recognition
uv run goodnotes read "Notebook name" --page 3 --source handwriting
```

The metadata reports counts for stored handwriting text and whether indexing is `pending`, `not-pending`, or `unknown`. Availability is `stored-text`, `no-stored-text`, or `unavailable`. `no-stored-text` describes this copy's index; it does not show that the page has no ink. An unavailable count is unknown, not zero. A pending-index warning may appear on `read` even when it returns no candidate text.

`read --source handwriting` returns stored recognition candidates and preserves their raw delimiters and alternatives. Label any cleaned reconstruction as an interpretation rather than an exact quote or visual transcription. `not-pending` does not verify freshness or accuracy. The CLI does not perform handwriting recognition or reconstruct ink from these candidates.

## Use a PDF exported by Goodnotes

Use a Goodnotes PDF export when you need the app's own rendering, including elements unsupported by native reconstruction:

1. In Goodnotes, export the notebook as a PDF with both **Include Annotation** and **Include Page Background** enabled. See [Goodnotes' PDF export instructions](https://support.goodnotes.com/hc/en-us/articles/360000629576-Include-Annotations-and-Page-Backgrounds-in-PDF-Exports).
2. Check that the exported PDF pages match the notebook's current page order. The CLI cannot make this check for you.
3. Register the PDF against the current local copy:

   ```sh
   uv run goodnotes attach-pdf "Notebook name" /path/to/notebook-export.pdf
   ```

4. Render a page or export the notebook using the `annotated` layer:

   ```sh
   uv run goodnotes render "Notebook name" --page 3 --layer annotated --output /tmp/page-3-annotated.png
   uv run goodnotes export "Notebook name" --layer annotated --output /tmp/notebook-annotated.pdf
   ```

Registration checks that the PDF opens and has the same page count as the notebook. It records the PDF's path and checks that the file has not changed before use, but does not copy the PDF. Keep the file at that path and do not modify it after registration. The CLI cannot verify that Goodnotes included annotations or that the page order matches. Annotated output is therefore marked as user-provided and unverified.

The registration belongs to the current synced copy. If a later `sync` creates a different copy, run `attach-pdf` again. A PDF that moves or changes also needs to be registered again.

## Read handwriting text

`attach-pdf` supplies pages for annotated rendering and export. It does not add the PDF's text to `read` or `search`.

The library may contain handwriting recognition candidates. The `pages --with-recognition` metadata helps locate pages with stored candidates or pending indexing. Read candidate text with:

```sh
uv run goodnotes read "Notebook name" --page 3 --source handwriting
```

These candidates are stored text, not a visual copy or a fresh transcription of the ink. `search` uses the stored text index, and `scan-pdfs` scans copied page backgrounds; neither searches the PDF registered with `attach-pdf`. Search does not launch `scan-pdfs` automatically.
