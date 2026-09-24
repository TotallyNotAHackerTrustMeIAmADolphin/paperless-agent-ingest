# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, Copilot, Gemini CLI, Aider, ...)
working in this repository. Rules come first, the reference material behind them follows.

**Also read `LOCAL.md` if it exists.** It is gitignored and holds everything specific to the
owner of this checkout: their Paperless instance, their naming conventions, what their
documents look like, and a log of past runs. Rules in `LOCAL.md` override defaults given here.
If it does not exist yet, create it from `LOCAL.md.example` on the first run.

## 1. What this project does

A cleanup pipeline for a Paperless-ngx instance with years of unsorted scans dumped into it.
Typical problems: double-sided scans with blank backs, rotated pages, one PDF bundling several
unrelated documents, one document split across several PDFs, nothing titled or tagged.

The pipeline pulls untouched documents out of Paperless, fixes them locally, and pushes them
back with a proper title, correspondent, document type, tags and date. **The classification is
done by you, the agent, reading the extracted text.** There is no LLM API call, no regex
matcher and no similarity model in the pipeline; Paperless's own classifier suggestions are in
the report as a hint, nothing more.

**Operating mode.** Default is fully automatic: decide, fix, push and clean up without waiting
for approval; ask only when something is genuinely unclear. The safety net is "never lose bytes
we already have" (originals stay under `work/`, Paperless keeps replaced files as versions and
deleted documents in its trash), not a manual gate. `LOCAL.md` may switch this to
"ask before `apply`".

## 2. How an ingest run goes

`run_ingest.bat` / `run_ingest.sh` start an agent with `docs/ingest-prompt.md`; the same steps
apply interactively.

1. `python -m pipeline.cli fetch` - downloads every untouched document into `work/inbox/<id>/`.
2. `python -m pipeline.cli prepare` - blank-page/rotation fix, OCR, per-page text, report.
3. Read `work/review/classification_report.json` and every `work/processed/<id>/content.md`.
   Classify by reading (section 3). For every document also check: bundled? misordered?
   pages missing? duplicate of something already filed?
4. Split/merge/reorder with `pipeline.pdf_tools` (`split_pdf`, `assemble_pages`, `merge_pdfs`)
   against `work/processed/<id>/ocr.pdf` (already OCR'd, keeps the text layer) into
   `work/processed/<key>/ocr.pdf` folders, one per output document.
5. Write `work/review/classifications.json`, one entry per output document:
   `{"doc_id", "source_doc_id"?, "title", "correspondent", "document_type", "tags": [...],
   "created": "YYYY-MM-DD"}`. `doc_id` names the processed folder (`"265a"`-style keys for
   split parts); `source_doc_id` is the Paperless id(s) the entry replaces (defaults to `doc_id`
   when numeric; a list for merges; several entries share one id for splits).
6. `python -m pipeline.cli apply`.
7. Run `fetch` again and repeat until it reports 0 untouched documents. New scans land in
   Paperless at any time during a long run and are only caught by a final re-fetch.
8. Before ending: record anything non-obvious in `LOCAL.md` (section 9).

## 3. Classification rules

**Title:** concise, in the language of the documents, pattern `<What> (<Who>) - DD.MM.YYYY`,
e.g. `Arbeitsvertrag (Musterfirma GmbH) - 01.09.2026`. All name fields (title, tag,
correspondent, document_type) are limited to 128 characters; `client._clip` truncates silently,
so keep titles short enough that a ` - duplicate suffix (see doc N)` still fits.

**Date (`created`):** the document's own date (signature line, letterhead date, "issued on"),
never the scan date and never the report's `created_guess` unverified. Paperless's date guesser
takes any date-shaped string: a birthdate, a cited law ("of 2 March 1974"), a policy start. For
undated attachments handed over together with a dated document, use that document's date and
say so.

**Names:** before creating a correspondent, tag or document type, check `client.list_*()` for a
near-match and reuse its exact spelling. New entities are created with `matching_algorithm: 6`
by the `get_or_create_*` helpers; never create them any other way (section 5). Never "correct"
an existing name; the instance's spelling conventions (umlauts vs. ASCII, abbreviations) belong
in `LOCAL.md`.

**Document types:** prefer broad reusable types (invoice, receipt, contract, certificate,
information letter, application form, questionnaire, protocol) over one-offs. The instance's
existing type names are the vocabulary; extend it only when nothing fits.

**Tags:** invent freely by life area (health, housing, car, insurance, energy, authorities,
work, ...), but only after no existing tag fits. Known inconsistencies of an instance (two tags
meaning the same thing) are recorded in `LOCAL.md`, not silently cleaned up.

**Missing pages (standing rule).** Feeder scanners sometimes skip a page or pull two sheets.
Watch while reading: a numbered list, section sequence or "point N" that jumps; "page N of M"
with gaps; a page opening mid-sentence with no plausible predecessor; a signature block without
an opening or vice versa. If found: do not fabricate, do not silently drop the document, tell
the user exactly what is missing, and say whether nearby blank-page drops (original page
numbering, section 4) could be the same gap. A corrective rescan from the user is merged into
the existing document (one entry, `source_doc_id: [old, rescan]`), not filed as a new one.

**Duplicates are tagged, never deleted.** Two Paperless documents that look like the same
underlying document (scanned twice, subset of pages, a second copy) both get the duplicate tag
named in `LOCAL.md` (default: `Duplikat-Verdacht`, "suspected duplicate") and a title suffix
pointing at the other document; the user deletes in the UI. Byte-identical files
(`exact_duplicate_of` in the report, or a rejected upload's `duplicate_of`) are near-certain
grounds; keyword hits (`possible_duplicates`) are a reason to read both. This is stricter than
the replace-yourself rule in `apply`, which only ever touches a document's own source ids.

**Bundles and order.** No automatic detector exists. Signals: letterhead, sender or date
changing between pages; "page N of M" footers resetting to 1; page stamps out of order. Those
stamps are tiny and OCR misreads them ("1/3" vs "2/3" once produced a confidently wrong order):
render at higher DPI and look. Pages with `low_confidence: true` or suspiciously empty text must
be rendered before deciding they are boilerplate: faint dot-matrix invoices OCR the letterhead
and lose the line items, which once hid a second invoice inside a 7-page scan. Multi-month
payroll exports and multi-copy onboarding packets are the recurring bundle cases.

## 4. Pipeline reference (`pipeline/`)

- `config.py` - `PAPERLESS_URL`, `PAPERLESS_TOKEN`, `OCR_LANGUAGES` from `.env`. Nothing else
  reads `os.environ`.
- `client.py` - the only HTTP code. Pins `Accept: application/json; version=10`
  (`API_VERSION`), sets a (10 s, 300 s) timeout, retries GET/PATCH/DELETE on 502-504 but never
  POST (a retried upload after a dropped response creates a duplicate). Functions:
  `list_documents(**filters)` (paginates; `fields=` keeps `content` out), `search_documents`,
  `find_by_checksum(sha256)`, `get_document`, `get_suggestions`, `download_document`,
  `upload_document` (returns a task UUID), `update_version(doc_id, file, label)` (new file
  version of an existing document, keeps id and metadata), `get_task` (normalized `status`;
  `related_document` is None on FAILURE because Paperless puts the *colliding* document there
  for rejected duplicates), `update_document` (PATCH; `tags` replaces the list, which is how a
  document leaves the inbox), `delete_document` (to trash), `list_tags/correspondents/
  document_types`, `get_or_create_*` (case-insensitive name match, else create with
  `matching_algorithm: 6`), `inbox_tag_ids`, `train_classifier`, `sha256_of`.
- `pdf_tools.py` - local PDF mechanics. `analyze_pdf` renders page by page (150 DPI, pdf2image)
  and flags `blank` (dark-pixel fraction < `BLANK_INK_THRESHOLD = 0.003`), `rotation`
  (tesseract OSD guess, accepted only if rotating raises mean OCR word confidence; the OSD
  score alone does not separate right from wrong guesses: wrong ones collapse confidence
  ~80 -> ~30, right ones lift it ~33 -> ~85; needs `osd` tessdata) and `low_confidence` (mean
  word confidence < 60). `fix_pdf` drops blanks and applies rotations, refusing to write a
  0-page PDF. `assemble_pages` (arbitrary/reordered subset, optional rotations), `split_pdf`
  (contiguous 0-indexed inclusive ranges -> `part_N.pdf`), `merge_pdfs`, `render_page`,
  `page_count`. **The blank threshold false-positives on thin-lined carbon-copy forms**: a fully
  legible single-page vehicle registration form once measured 0.0026. Whenever every page of a
  short document is "blank", render and look before believing it. `crop_page`/
  `build_pdf_from_images` recover multiple logical A4 pages out of one oversized scan (an A3
  scanner tray, or a folded certificate/booklet scanned open flat): render, crop to a
  fractional box, rotate, rebuild a raster-only PDF, then `ocr.run_ocr` it for a text layer
  scoped to just that page - a `/MediaBox` crop alone would leave the other half's OCR words in
  the shared content stream, still extractable as this page's text.
- `form_fill.py` - overlays text/checkmarks/a signature onto a flat form (scanned image + OCR
  text layer, or a born-digital layout with no fillable fields) when a document needs filling in
  rather than just classifying, e.g. a blank onboarding questionnaire or self-disclosure form
  that came back into the inbox.
  - For a **table cell**: get the cell's full box first - `detect_row_lines` crossed with
    `detect_col_lines` (its vertical counterpart) finds all four walls by rendering and looking
    for image rows/columns that are mostly dark, since `page.get_drawings()` finds nothing on a
    page that is one embedded raster with no vector paths. Then `fill_centered(page, cell_rect,
    text)` picks the largest font that fits and centers the text both ways - no offset to get
    right, because every wall is already known. Prefer this over placing text from one corner
    outward with a guessed offset: in practice that approach needed several separate rounds of
    fixes (text sitting on a gridline, a value crossing the row below it, a signature clamped to
    roughly half its intended height) that centering in a known box avoids by construction.
  - For anything that isn't a table cell (a labeled single-line field, a checkbox, the line above
    a signature): `find_label` locates a label by word match instead of hardcoded coordinates -
    but a short/common label is not automatically unique (searching a page for 'Von' once
    silently matched the "von" inside an unrelated earlier sentence and placed a whole table's
    dates under the wrong header with no error); pass `after_y` to disambiguate, and prefer the
    longest substring that's still exactly what's printed. `fit_font_size`/`place_text` pick the
    largest single-line font that fits a given width (prefer that over guessing a size and
    re-rendering, and over shrinking below `MIN_LEGIBLE_SIZE` - wrap to a second line at normal
    size instead). `detect_row_lines` alone (without a matching `detect_col_lines`) still finds a
    single line's position directly, e.g. the rule above a signature, when reasoning from a
    nearby label's offset alone isn't reliable enough.
  - `insert_signature` sizes a signature from its real aspect ratio instead of a hand-picked rect
    (which silently clamps to whichever of width/height is tighter). `preview` renders pages for
    the fill -> render -> look -> adjust loop this module exists to shorten - still do this even
    with `fill_centered`, its vertical centering is an approximation (PDF text positions by
    baseline, not a centered box) tuned for Helvetica at ordinary form-entry sizes.
  - Nothing in this module talks to Paperless; `client.update_version` uploads the result,
    `client.wait_for_task` polls the task. Filling in someone's signature is sensitive: do it
    only with the document owner's standing, explicit permission for reuse (not inferred from one
    past one-off case), and never source a signature for use on a different person's document.
- `ocr.py` - `needs_ocr` (< 20 extractable chars/page on average), `run_ocr` (ocrmypdf with
  `OCR_LANGUAGES`, `skip_text=True`, deskew; copies born-digital files through untouched, since
  re-OCRing a Tagged PDF only destroys its structure), `extract_text` (pymupdf per page,
  reading-order sorted, whitespace collapsed, a `--- Page N of M ---` line before every page;
  page N of `content.md` is index N-1 of `ocr.pdf`).
- `cli.py` - three stages behind a per-stage lock file (`work/.stage.lock`, stale after 3 h):
  - `fetch`: `GET /api/documents/?is_in_inbox=true` plus a fallback query for documents with
    neither correspondent nor document_type (legacy uploads from before the inbox tag). Writes
    `work/inbox/<id>/original.pdf` and `meta.json` (metadata snapshot without `content`).
    Raw image uploads (JPEG/PNG/TIFF...) are converted to a one-page PDF in place (a `.jpeg`
    saved as `.pdf` once crashed `prepare` with "unable to find trailer dictionary").
  - `prepare`: skips every `work/inbox/` folder whose id is not in the *live* untouched set
    (same query as `fetch`); `meta.json` is only the metadata snapshot for the report, never
    the untouched check (a stale snapshot still carrying the inbox tag once re-OCR'd an
    already-applied 14-page bundle on every run); born-digital files skip the render/OSD
    pass entirely; everything per document sits inside one try/except so a corrupt file prints
    `FAILED preparing <id>` and the rest of the report survives (keep every `pdf_tools`/`ocr`
    call inside it). Writes `content.md`, `fixed.pdf`, `ocr.pdf` and the report.
  - `apply`: see the docstring in `cli.py` for the exact rules. One entry with one source ->
    `update_version` + PATCH (marker `ocr.versioned.pdf`, nothing deleted). One entry, one
    source, bytes unchanged (born-digital, nothing to fix) -> PATCH only (`ocr.patched.pdf`).
    One entry, several sources (merge) -> version of the first, delete the others after success.
    Several entries sharing a source (split) -> upload each part, force the tag set back to the
    entry's list (Paperless adds the inbox tag and classifier guesses on consumption), delete
    the bundle only after every part succeeded (`ocr.uploaded.pdf`). Markers make a rerun skip
    finished entries; versioned/patched sources are never deleted on any run. Per-entry
    failures are isolated so the deletion pass still runs for entries that did succeed.
- `selftest.py` - `python -m pipeline.selftest`: every write path against the live instance with
  throwaway documents and a throwaway tag/type (upload, tag forcing, PATCH, checksum lookup,
  duplicate rejection, `update_version`, download, `apply()` version and split paths, delete).
  Run after touching `client.py`/`apply`, after a Paperless upgrade, on a fresh machine.
- `check_connection.py` - smoke test of `.env`.

**Reading `classification_report.json`.** Per document: `created_guess` (Paperless's guess, see
section 3), `pages_before`/`pages_kept`, `dropped_pages`/`rotated_pages`/`low_confidence_pages`
**0-indexed against the original file**; `content.md`/`ocr.pdf` are numbered after the drops
(drops `[5, 7, 15]` move flagged pages `[4, 50, 51]` to `[3, 46, 47]`: count the drops below
each flagged page), `exact_duplicate_of`, `possible_duplicates` (10 long words through
Paperless full-text search, classified hits only), `suggestions` (Paperless classifier:
correspondents, document_types, tags, dates, resolved to names), `text_excerpt`.

## 5. Paperless-ngx facts the pipeline relies on

Verified against Paperless-ngx 3.1.0 / API v10 (`X-Version` / `X-Api-Version` response
headers); the selftest also passes on 3.2.1. `docs/review-2026-09-22.md` (German) has the
sources.

- **The inbox tag is the only reliable "not yet classified" signal.** Paperless's auto-matcher
  assigns correspondent, document_type and tags on consumption, so their presence proves
  nothing. Only an explicit PATCH with a tag list clears the inbox tag. If the untouched check
  is ever rewritten to key on anything else, documents get skipped (happened twice).
- **`is_in_inbox=true` is the API filter.** `tags__is_inbox_tag=true` is not; Paperless ignores
  unknown query params silently and returns everything. Check new filter names against
  `GET /api/schema/?format=json`. `title_content` is deprecated in v10; use `text` /
  `title_search`.
- **Auto-matching (`matching_algorithm=6`) only trains on entities created with 6.** Upstream
  `documents/classifier.py`: training set = documents without inbox tag; a document is a
  positive example for a correspondent/type/tag only if that entity itself is MATCH_AUTO,
  otherwise it contributes nothing. The API default (1, "Any word") is useless for this.
  Retrain on demand: `client.train_classifier()` (`POST /api/tasks/run/
  {"task_type": "train_classifier"}`; other task types: `sanity_check`, `index_optimize`,
  `empty_trash`, `check_workflows`, ...).
- **Byte-identical uploads may be rejected**: task `failure`, `result_data.duplicate_of` = the
  existing document (behaviour of `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true`; the upstream
  default consumes with a warning). `apply` avoids it by hashing; ask `checksum__iexact=<sha256>`
  first when unsure. `more_like_id` returned nothing on the instance this was built against.
- **Document versions** (`POST /api/documents/{id}/update_version/`, multipart `document`,
  `version_label` <= 64): same id, metadata kept, `content` re-extracted, old file under
  `versions`; the new version's id (in `related_document_ids`) is not a standalone document
  (GET 404, absent from listings); deleting the root deletes all versions. Server-side PDF
  editing exists too (`/api/documents/edit_pdf/`, operations `{"page", "rotate", "doc"}`) but
  returns no ids, so splitting stays local.
- **Archive files:** with the default `PAPERLESS_ARCHIVE_FILE_GENERATION=auto` Paperless skips
  PDF/A for files that already carry text, i.e. everything this pipeline pushes. `download/`
  without `?original=true` then serves the original. `content` is the text source.
- **Task objects (v10):** paginated `/api/tasks/`, lower-case `status`, `related_document_ids`,
  `result_data`, `task_type`. `client.get_task` normalizes. `client._url` already prepends
  `/api/`, so calling it with a leading-slash path (`_url("/api/tasks/")`) silently double-prefixes
  to `/api/api/tasks/`, which Paperless's SPA catch-all answers with a 200 HTML login page, not a
  404 - pass bare paths (`_url("tasks/")`). Listing `task_type=consume_file` tasks and filtering
  for `status=failure` is the way to explain a scan the user says they made that never shows up
  as a Paperless document: a folder-watch consumer that grabs a file mid-write fails with
  `ConsumerError: ...: InputFileError:` (empty detail) and the file is simply gone from Paperless's
  view - `fetch`/`prepare` have no way to see it since it was never a document at all.
- **Suggestions endpoint** `GET /api/documents/{id}/suggestions/` returns classifier ids and
  candidate dates; `prepare` resolves them into the report.
- **Server-side settings worth suggesting to the user (env only):** `PAPERLESS_IGNORE_DATES`
  for recurring false-positive dates such as a birthdate; barcode separator sheets
  (`PAPERLESS_CONSUMER_ENABLE_BARCODES`) to stop bundled scans at the source;
  `PAPERLESS_ARCHIVE_FILE_GENERATION=always` if PDF/A archives are wanted.

## 6. Setup and environment

See `README.md` for the full install. Points that bite agents:

- `.env` holds `PAPERLESS_URL`, `PAPERLESS_TOKEN`, optionally `OCR_LANGUAGES`. Never print,
  commit or copy the token anywhere.
- `ocrmypdf` needs Tesseract (with the `osd` language pack plus every language in
  `OCR_LANGUAGES`), Ghostscript and poppler (`pdftoppm`) on `PATH`. On Windows, `TESSDATA_PREFIX`
  must point at a folder that also contains Tesseract's `configs` and `tessconfigs`
  subfolders, or ocrmypdf fails with `TesseractConfigError: ... Can't open hocr`.
- User-level PATH/env changes only reach shells started afterwards.
- The runner scripts must activate `.venv` before launching the agent; an interactive session
  that falls back to `.venv/Scripts/python` explicitly means that line got lost.
- `pymupdf` is used for text extraction and (as `fitz`) for form filling; `pytesseract`,
  `pdf2image`, `pikepdf`, `pypdf`, `ocrmypdf`, `Pillow`, `requests`, `python-dotenv`.

## 7. `work/` directory and concurrency

`work/` is gitignored scratch and holds real personal documents. `work/inbox/<id>/` original +
meta.json; `work/processed/<key>/` fixed.pdf, ocr.pdf (renamed to the marker on success),
content.md; `work/review/` report + classifications. Paperless is the source of truth once a
document is pushed.

A scheduled runner may run concurrently with an interactive session. Each stage takes
`work/.stage.lock` (exits with a message if another stage holds it, takes over locks older
than 3 h). That stops two `prepare`/`apply` runs overwriting `work/review/*.json`, but not two
sessions interleaving stages. If `work/review/*` looks different from what you expect, check
Paperless before assuming data loss; a concurrent run probably finished the job.

## 8. Related material

- `LOCAL.md` (gitignored): the owner's instance, conventions, document knowledge, incident log.
- `docs/ingest-prompt.md`: the prompt the runner scripts hand to the agent.
- `docs/review-2026-09-22.md` (German): Paperless 3.1 capabilities vs. this pipeline and the
  reasoning behind the current design.

## 9. Learning from each run

Every runner-started session starts with no memory of earlier ones, and an agent's own memory
store is tied to one machine and not in git. Anything a future session on this checkout needs
(a Paperless or pipeline quirk, a naming decision, a data-quality finding, a classification
judgment worth repeating) goes **into `LOCAL.md`**, with concrete ids and dates as evidence.
Anything that is true for every Paperless instance goes into this file instead, in the section
it belongs to, in generic wording. Before ending a session, check whether anything came up
that is not already written down.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (`gh` CLI); the repo is public, so tickets carry no
instance details. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`,
`wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root, created lazily. See
`docs/agents/domain.md`.
