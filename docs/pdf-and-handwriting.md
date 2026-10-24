# PDF export and handwriting

The CLI keeps page backgrounds separate from Goodnotes annotations. A PDF background attachment contains the notebook's source page. It does not prove that the note's handwriting is present.

## Background export

`export_pdf(..., layer="background")` and `render_page(..., layer="background")` use each page's own `attachment_path`. They sort pages by their one-based notebook page number and use the one-based `pdf_page` value for multi-page attachments. This preserves the attachment assigned to each page when the notebook order changes.

The background layer supports PDF and image attachments. If an attachment is missing, encrypted, or has an ambiguous page mapping, the command returns an error. It does not create a blank page or claim that handwriting is included. Result metadata sets `includes_handwriting` to `false` and includes a warning. Marks already printed into an imported source PDF remain part of that background and cannot be separated here.

Exports and PNG renders use `Sandbox.ensure_output` and fail when the destination already exists. The Python render function accepts 72 to 600 DPI and rejects pages that would exceed 50 million pixels. The CLI limits `render` and `bundle` to 72 to 300 DPI.

For example, use `uv run goodnotes export NOTEBOOK --output notebook.pdf` for backgrounds. After registering a Goodnotes export, add `--layer annotated` to export or render that PDF layer. `uv run goodnotes bundle NOTEBOOK --pages 1-3 --output pages/ --layer annotated` writes page images and the CLI's source-labeled text data for those pages.

## Annotated PDF import

The CLI does not decode Goodnotes' native drawing data. The copied `projection.sqlite` has no `notes_items` rows, while `notes_main.db` uses LevelDB files that this project does not parse. Background attachments cannot stand in for that missing annotation layer.

To include handwriting, export the notebook as a PDF in Goodnotes with both **Include Annotation** and **Include Page Background** enabled. Goodnotes documents these options in its [PDF export instructions](https://support.goodnotes.com/hc/en-us/articles/360000629576-Include-Annotations-and-Page-Backgrounds-in-PDF-Exports). To make handwriting searchable in the exported PDF, choose a flattened export and enable handwriting recognition, as described in [Goodnotes' PDF format guide](https://support.goodnotes.com/hc/en-us/articles/8537070839183-Choose-Between-Editable-and-Flattened-PDF-Export).

`register_annotated_pdf(sandbox, document_id, pdf_path, expected_pages)` checks that the notebook in the current sandbox and the PDF have the same page count. It records the source path, file hash, and page count in a private per-notebook sidecar beside the sandbox snapshot. It does not copy the PDF, so keep it at that path. If the file changes or moves, register it again.

From the CLI, register with `uv run goodnotes attach-pdf NOTEBOOK exported.pdf`.

Page count does not verify page order. Before registration, confirm that the exported PDF pages match the notebook's current order. Later, `layer="annotated"` selects the PDF page with the same one-based number as the notebook page. The CLI returns an error when there is no valid registration; it never substitutes the background layer. Annotated-layer metadata sets `includes_handwriting` to `null` and `annotation_status` to `user-provided-unverified`. The CLI cannot inspect whether annotations were included.

## Agent-readable handwriting

Use a searchable flattened PDF when text extraction should include Goodnotes handwriting. The user-exported PDF may provide that text when handwriting recognition was enabled. The CLI does not run handwriting recognition itself. A rendered PNG is useful for visual review, but its pixels are not searchable text. Keep the registered PDF alongside any page images or extracted text sent to an agent.

The `attach-pdf` command registers the PDF for visual rendering and page export. It does not extract text from that PDF. `goodnotes read NOTEBOOK --source handwriting` returns the recognition candidates already stored in the sandbox search database. `bundle` combines those source-labeled candidates with the selected page images.
