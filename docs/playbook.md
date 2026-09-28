# Playbook

The recurring processes of this repo: which tool to use when, what "finished" means, and how the
work is controlled. `AGENTS.md` holds the rules and reference; this file is the order of work.
Commands run from the repo root with `.venv` active: `python -m pipeline.cli <command>`.
Instance-specific names and conventions live in `LOCAL.md` / `LOCAL_KNOWLEDGE.md`.

This file is also the prompt the runner scripts (`run_ingest.bat` / `run_ingest.sh`) hand to the
agent: an unattended run reads it first, then `LOCAL.md` and `LOCAL_KNOWLEDGE.md` (not
`LOCAL_LOG.md`, see the note at the top of `AGENTS.md`), and carries out P1 completely. Work in the
operating mode `LOCAL.md` specifies (default: fully automatic, ask only when something is
genuinely unclear). Do not rely on any agent memory system; the next session has no access to
the chat.

## Tool map

| Need | Use |
|---|---|
| Pull untouched documents | `fetch` |
| Fix, OCR, extract text, build the report | `prepare` |
| Overview of the report, one block per document | `report` |
| Is the new text the same as a filed document? | `compare NEW OLD` |
| Tag and retitle the already-filed side of a duplicate | `mark-duplicate NEW OLD` |
| Split, reorder, merge pages | `pipeline.pdf_tools`: `split_pdf`, `assemble_pages`, `merge_pdfs` |
| Look at a page | `pdf_tools.render_page`, `form_fill.preview`, then Read the PNG |
| Crop part of a page, rebuild a PDF from crops, give it a text layer | `pdf_tools.crop_page`, `build_pdf_from_images`, `ocr.run_ocr` |
| Find a printed label on a page | `form_fill.find_label` |
| Find a scanned table's gridlines | `form_fill.detect_row_lines`, `detect_col_lines` |
| Put a value into a known cell | `form_fill.fill_centered` |
| Put a value after a label, check a box | `form_fill.text_width`, `fit_font_size`, `place_text`, `mark_checkbox` |
| Same on a rotated scan (`/Rotate`) | `form_fill.to_visible`, `place_text_visible` |
| Sign | `form_fill.insert_signature` (permission rule in P6) |
| Push a changed file back | `client.update_version`, `client.wait_for_task` |
| Push results back | `apply` |
| Anything else against Paperless | `pipeline.client`, never raw HTTP |

Why each helper works the way it does is in its docstring; read that before using one. Procedures
for changing or creating documents (fill, crop, sign, upload) are in `docs/playbook-documents.md`,
opened only when P6 or an oversized scan calls for it.

Reading each `content.md` is a manual step and cannot be replaced by a command. Classification is
your judgement, not a tool's.

## P1 Normal ingest run

1. Read `LOCAL.md` and `LOCAL_KNOWLEDGE.md`. `fetch`, then `prepare`. `prepare` skips folders that are no longer untouched, that is normal.
2. `report` for the overview, then read every `work/processed/<id>/content.md` in full.
3. Per document decide: title, correspondent, type, tags, date (`AGENTS.md` section 3). Check
   every document for bundle, order, missing pages, duplicate (P2 to P4).
4. Write `work/review/classifications.json` (UTF-8 **without BOM**; Windows PowerShell 5.1
   `Set-Content -Encoding utf8` adds one and `apply` fails, write it from Python), one entry per
   output document.
5. `apply`.
6. `fetch` again. Repeat from 1 while it reports more than 0 untouched documents.
7. Close out with P9.

**Finished when:** the last `fetch` reports 0 untouched documents, `apply` printed no failure, and
every finding (gap, duplicate, open question) is reported to the user.

## P2 Suspected duplicate

Trigger: `exact_duplicate_of` is set, or `possible_duplicates` lists a classified document.

1. An empty `exact_duplicate_of` proves nothing for born-digital files: a portal renders a new
   PDF on every download, so the same letter has different bytes each time. Compare content, not
   bytes: `compare NEW OLD` for each classified candidate in `possible_duplicates`. SAME is strong evidence, SIMILAR means read both, DIFFERENT
   means not a duplicate. Recurring letters (yearly meter request, yearly invoice) differ in dates
   and stay separate documents.
2. If duplicate: `mark-duplicate NEW OLD` for the filed side. For the new side, put tag
   `Duplikat-Verdacht` and the printed title suffix into its `classifications.json` entry.
3. Never delete. The user decides in the UI.

**Finished when:** both sides carry the tag and a suffix pointing at each other. If the document an
old suffix points at is gone (GET 404), `mark-duplicate` repoints it at the new one.

## P3 Bundle or wrong order

Signals are in `AGENTS.md` section 3 (letterhead or date change, footers restarting at 1, stamps
out of order). Render the page and look, do not trust OCR of tiny stamps.

1. Split or reorder from `work/processed/<id>/ocr.pdf` into `work/processed/<key>/ocr.pdf`
   (keys like `265a`).
   A page that holds several logical pages side by side (an oversized or folded scan) is cropped
   first, see `docs/playbook-documents.md`.
2. One `classifications.json` entry per output, each with `source_doc_id` of the bundle.

**Finished when:** every part has its own entry and the page count of all parts matches the source
minus intentional drops.

## P4 Missing pages

Watch for jumps in numbering, a page opening mid-sentence, a signature block without an opening.
Do not fabricate and do not drop the document. Report to the user exactly what is missing and
whether a blank-page drop in the report could be the same gap. A rescan later is merged as one
entry with `source_doc_id: [old, rescan]`.

**Finished when:** the gap is named in the final message to the user.

## P5 Rescan or second copy to merge

One entry, `source_doc_id` a list. `apply` versions the first and deletes the others after success.

## P6 Form to fill in

Trigger: a blank form or questionnaire in the inbox that the owner has to submit. Fill only forms
the owner submits themselves; the signature permission and its limits are in `LOCAL_KNOWLEDGE.md`.
Leave missing facts empty and report them. The procedure (fill, crop, sign, upload) is in
`docs/playbook-documents.md`; open it now.

**Finished when:** `docs/playbook-documents.md` says so for the procedure used.

## P7 Something failed

| Symptom | Action |
|---|---|
| "another pipeline stage is running" | Wait. Only delete `work/.stage.lock` if that run is dead. |
| `FAILED preparing <id>` | Read the error, fix the input, rerun `prepare`. The rest of the report is fine. |
| `apply` failure on one entry | Others still finished. Fix that entry and rerun `apply`, markers skip finished ones. |
| `work/review` looks different than expected | Check Paperless first, a concurrent run may have finished the job. |
| A scan is missing from Paperless | List `consume_file` tasks with status failure, see `AGENTS.md` section 5. |

## P8 Change to the pipeline code

Run `python -m pipeline.selftest` after touching `client.py` or `apply`, after a Paperless upgrade
and on a fresh machine. Offline helpers have tests in `tests/` (`pip install pytest`, then `python -m pytest tests`). Keep `AGENT_HANDOFF.md` in step with
`client.py` by hand.

## P9 Closing out a run

Check whether anything came up that is not written down, then file it by the rule in `AGENTS.md`
section 9: standing rule to `LOCAL.md`, reusable classification knowledge to `LOCAL_KNOWLEDGE.md`,
this run's record to `LOCAL_LOG.md`, true for every instance to `AGENTS.md`.

## How the work is controlled

- **Source of truth:** Paperless. `work/` is scratch with real personal data, never committed.
- **Never lose bytes:** originals stay under `work/`, replaced files become versions, deletions go
  to the trash for 30 days. Duplicates are tagged, never deleted.
- **Stage lock:** `fetch`, `prepare`, `apply` take `work/.stage.lock`. The read-only commands do not.
- **Markers:** finished `apply` entries leave a marker file, so a rerun is safe.
- **Operating mode:** automatic by default. `LOCAL.md` may switch to asking before `apply`.
- **Verification is part of finishing:** re-fetch to 0, look at rendered output for anything you
  changed, and report what you could not verify.
