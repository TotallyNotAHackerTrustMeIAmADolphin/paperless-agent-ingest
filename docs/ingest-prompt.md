# Ingest run

Carry out the full ingest process for the Paperless scans in this project, as described in
`AGENTS.md` (and `LOCAL.md` if present; read both first):

1. Run `python -m pipeline.cli fetch`, then `python -m pipeline.cli prepare`.
2. Read `work/review/classification_report.json` and every `work/processed/<id>/content.md`.
   Decide title, correspondent, document type, tags and date for every document yourself by
   reading the extracted text (no separate LLM API call) and write
   `work/review/classifications.json`.
3. Watch for bundled PDFs (several independent documents in one scan) and for pages out of
   order; use `pipeline.pdf_tools` (`split_pdf`, `assemble_pages`, `merge_pdfs`) to split or
   reorder before uploading.
4. Check every document for missing pages (numbering, "page N of M", sentences starting
   mid-way) and report gaps instead of ignoring them.
5. Do NOT delete suspected duplicates; tag them with the duplicate tag from `LOCAL.md`
   (default `Duplikat-Verdacht`) and add the title suffix pointing at the other document.
6. Run `python -m pipeline.cli apply`.
7. Repeat `fetch` at the end until nothing new arrives.
8. Work in the operating mode `LOCAL.md` specifies (default: fully automatic, no check-ins;
   ask only when something is genuinely unclear).
9. Before finishing, follow the "Learning from each run" rule: write anything non-obvious that
   came up (bugs, naming decisions, classification judgments, data-quality findings) into
   `LOCAL.md`, or into `AGENTS.md` if it is true for every Paperless instance. Do not rely on
   any agent memory system; the next session has no access to this chat.
