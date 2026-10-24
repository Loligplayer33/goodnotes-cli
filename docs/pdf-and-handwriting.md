# PDFs and handwriting

The CLI can export the page backgrounds stored in the Goodnotes library. It cannot rebuild native Goodnotes ink or run OCR on images. To see annotations, provide a PDF exported from Goodnotes and register it for the current local copy.

## Export page backgrounds

The default `background` layer uses the PDF or image attachment assigned to each page:

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

## Export with annotations

Use a Goodnotes PDF export when you need to see the notebook's ink:

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
