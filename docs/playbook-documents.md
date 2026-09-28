# Playbook: changing and creating documents

Continues `docs/playbook.md` (P1 to P9) with P10 to P15: changing a document's pages or content.
Read on demand, when P3 or P6 in the ingest playbook sends you here. The tool map is in
`docs/playbook.md`; why each helper behaves as it does is in its docstring.

The rule behind all of it: **never guess coordinates.** Find them (label search, gridline
detection, measured text width), place, render, look, adjust.

Work on copies under `work/qa/<id>/` (gitignored). The original stays in Paperless as a version
after upload.

## P10 Fill a flat form (scan, or born-digital layout without fields)

1. Copy the source (`ocr.pdf` or the downloaded original) to `work/qa/<id>/`.
2. **Look first.** `preview` the page and read it. Decide every value before placing anything.
   Missing information is left empty and reported, never invented.
3. Per field, use the cheapest reliable anchor:
   - Table cell: row lines crossed with column lines, then `fill_centered`. Check that the line
     counts match the rows and columns you see.
   - Labeled line or checkbox: `find_label` with the longest exact string, `after_y` for short or
     common words, then `fit_font_size` and `place_text` / `mark_checkbox`.
   - Rotated scan: `to_visible` on found labels, `place_text_visible` for the value.
   - Line above a signature: row-line detection on a narrow x range.
4. Prefer one line in a smaller font over a wrapped line; wrap only when even the minimum
   legible size does not fit.
5. Save a new file, `preview` at 200 DPI, and **look at the changed areas** against the blank
   form. Fix the fields that are off and repeat. No upload before a render was inspected.

## P11 Fill a born-digital form that has real fields

`page.widgets()` lists them: set `field_value`, choose a font size that does not overflow (7 to 9),
`update()`. Text without a field goes in with `insert_text`. Render and check, since some viewers
do not draw values that lack an appearance stream.

## P12 Recover pages from an oversized or folded scan

1. Render and look. Decide fractional boxes (left half is `(0, 0, 0.5, 1)`) and the rotation.
2. `crop_page` per logical page, `build_pdf_from_images`, then `ocr.run_ocr` so each page's text
   layer covers only what is on it. A media-box crop alone leaves the other half's words in the
   content stream.
3. Render the result: reading order right, nothing cut off.

## P13 Reorder, drop, rotate, split, merge

Same as P3 in `docs/playbook.md`: `assemble_pages`, `split_pdf`, `merge_pdfs`. Render pages whose
order rests on tiny stamps.

## P14 Sign

Only documents the owner submits themselves, using the standing permission and signature files in
`LOCAL_KNOWLEDGE.md`. Never sign for another person. Legally sensitive documents (contracts with
third parties, declarations to authorities): ask before signing. Use `insert_signature` with a
transparent variant, then render and check that it sits on the line and covers no printed text.

## P15 Upload and record

1. `client.update_version(doc_id, path, label)` with a label naming the round (at most 64
   characters), then `client.wait_for_task`. A failed task is reported, not retried blindly.
2. `client.get_document` and check that `content` contains the new values.
3. A filled copy the owner keeps as its own document goes through `classifications.json` and
   `apply` instead.
4. Correction rounds are new versions, all kept; say which one is final.
5. File what came up by `AGENTS.md` section 9.

**Finished when:** a render of the final file was inspected, the version is in Paperless, its
`content` shows the values, and every field left empty or assumed is named to the user.

## Adding a tool

When a task needs the same manual measuring twice, make it a function in `pipeline/form_fill.py`
or `pipeline/pdf_tools.py` whose docstring says what went wrong by hand, add an offline test in
`tests/` if it can run without Paperless, and add it to the tool map in `docs/playbook.md`.
