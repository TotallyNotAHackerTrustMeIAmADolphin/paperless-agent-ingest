# paperless-agent-ingest

Clean up a [Paperless-ngx](https://docs.paperless-ngx.com/) archive full of unsorted scans with
a coding agent doing the reading.

The pipeline fetches every document that is still in the Paperless inbox, fixes it locally
(drops blank backs, straightens rotated pages, OCRs it, extracts the text page by page) and
writes a report. A coding agent (Claude Code, Codex, Cursor, Copilot, Gemini CLI, Aider, ...)
then reads the text, decides title, correspondent, document type, tags and date, splits bundled
scans and merges split ones, and the pipeline pushes the result back into Paperless as a new
file version of the same document. No LLM API call inside the pipeline, no separate classifier
to train: the agent you already use is the classifier.

```
Paperless inbox ──fetch──▶ work/inbox/<id>/original.pdf
                            │
                          prepare   (blank pages, rotation, OCR, per-page text, report)
                            │
                            ▼
                 work/processed/<id>/{fixed.pdf, ocr.pdf, content.md}
                 work/review/classification_report.json
                            │
                     the agent reads, splits, merges, decides
                            │
                            ▼
                 work/review/classifications.json
                            │
                          apply     (update_version / upload, PATCH metadata, leave inbox)
                            │
                            ▼
                     Paperless, classified
```

Built and tested against Paperless-ngx 3.1.0 and 3.2.1 (REST API v10, pinned in
`pipeline/client.py`).
German documents were the original use case, but nothing is German-specific except the
default OCR languages and title convention, both of which are configurable.

## Requirements

- Python 3.11 or newer
- [Tesseract](https://tesseract-ocr.github.io/) with the `osd` pack and every language in
  `OCR_LANGUAGES` (default `deu+eng`)
- [Ghostscript](https://ghostscript.com/) and poppler (`pdftoppm`) on `PATH` (used by ocrmypdf
  and pdf2image)
- A Paperless-ngx instance and an API token (Settings -> My Profile -> API Token)
- A coding agent that can read files and run shell commands in this directory

## Setup

```sh
git clone https://github.com/TotallyNotAHackerTrustMeIAmADolphin/paperless-agent-ingest
cd paperless-agent-ingest
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in PAPERLESS_URL and PAPERLESS_TOKEN
cp LOCAL.md.example LOCAL.md                     # optional: your instance's conventions
cp LOCAL_KNOWLEDGE.md.example LOCAL_KNOWLEDGE.md # optional: your correspondents/patterns
cp LOCAL_LOG.md.example LOCAL_LOG.md             # optional: incident log, read on demand only
python -m pipeline.check_connection
python -m pipeline.selftest     # exercises every write path with throwaway documents
```

### System packages

**Debian/Ubuntu**

```sh
sudo apt install tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd \
                 ghostscript poppler-utils
```

**macOS (Homebrew)**

```sh
brew install tesseract tesseract-lang ghostscript poppler
```

**Windows**

- Tesseract: `winget install --id UB-Mannheim.TesseractOCR -e`. The installer ships `eng`
  only. Put `deu.traineddata`, `eng.traineddata` and `osd.traineddata` (from
  [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)) into a folder you own, copy
  Tesseract's `tessdata\configs` and `tessdata\tessconfigs` folders next to them, and set the
  user environment variable `TESSDATA_PREFIX` to that folder. Without the two config folders
  ocrmypdf fails with `TesseractConfigError: ... Can't open hocr`.
- Ghostscript: installer from
  [ghostpdl-downloads](https://github.com/ArtifexSoftware/ghostpdl-downloads/releases), then
  add its `bin` folder to `PATH`.
- poppler: `winget install poppler` (or any build that puts `pdftoppm.exe` on `PATH`).
- Environment changes reach only shells started afterwards.

### `.env`

| Variable          | Meaning                                                    |
|-------------------|------------------------------------------------------------|
| `PAPERLESS_URL`   | Base URL of the instance, no trailing slash                |
| `PAPERLESS_TOKEN` | API token                                                  |
| `OCR_LANGUAGES`   | Tesseract language string for ocrmypdf, default `deu+eng`  |

## Running an ingest

Manually, stage by stage:

```sh
python -m pipeline.cli fetch      # inbox documents -> work/inbox/<id>/
python -m pipeline.cli prepare    # fix, OCR, extract text, write the report
# ... the agent reads work/review/classification_report.json and work/processed/*/content.md,
#     splits/merges with pipeline.pdf_tools, writes work/review/classifications.json ...
python -m pipeline.cli apply      # push back to Paperless
```

With an agent, unattended: `run_ingest.sh` (any agent with a CLI; set `AGENT`, default
`claude`) or `run_ingest.bat` (Claude Code on Windows). Both activate the venv and hand the
agent `docs/ingest-prompt.md`. For an agent without a CLI, open the repository in it and paste
that prompt.

The agent's rules live in `AGENTS.md`: the workflow, the classification rules (dates, names,
duplicates, missing pages, bundles), the pipeline reference and the Paperless behaviours the
pipeline depends on. `CLAUDE.md` just points at it. Everything specific to *your* instance and
*your* documents is gitignored and split across three files, so the agent isn't re-reading its
whole history on every run: `LOCAL.md` (standing conventions) and `LOCAL_KNOWLEDGE.md` (your
correspondents and document patterns) are read every run and are where the agent writes back
what it learns; `LOCAL_LOG.md` is a chronological incident log the agent only consults on
demand, not on every run.

## Filling in blank forms

Some inbox documents are not just scans to classify but blank forms - an onboarding
questionnaire, a self-disclosure form - that need answers written in before they go back into
Paperless. `pipeline/form_fill.py` overlays text, checkmarks and a signature onto a flat form (a
scanned image with an OCR text layer, or a born-digital layout with no fillable fields) without
hardcoding pixel coordinates by hand: `find_label` locates a field by its OCR text instead of a
guessed position, `fit_font_size`/`place_text` pick the largest font that still fits on one line,
`detect_row_lines` finds a scanned table's gridlines by looking for dark image rows (there are no
vector lines to find - the page is one embedded image), and `insert_signature` sizes a signature
from its own aspect ratio instead of a hand-picked box. There is no pipeline stage for this: it's
a library the agent calls from an interactive session (fill a field, `form_fill.preview` the
result, adjust, repeat, then `client.update_version` once it looks right). See `AGENTS.md` for
the full API, and keep a signature's reuse permission and any document-specific notes in
`LOCAL_KNOWLEDGE.md`, never in a committed file.

## Safety properties

- Nothing is ever deleted before its replacement is confirmed in Paperless. A fixed file
  becomes a new **version** of the same document (id and metadata stay, the old file stays
  retrievable). Only bundles that were split into several new documents are deleted, after
  every part succeeded, and only into Paperless's trash.
- Suspected duplicates are tagged, never deleted.
- `work/` keeps every original and is gitignored, as are `.env`, `LOCAL.md`,
  `LOCAL_KNOWLEDGE.md` and `LOCAL_LOG.md`. Do not commit them; they contain your documents and
  your token.
- Each stage takes a lock file so a scheduled run and an interactive session cannot corrupt
  each other's report.

## Repository layout

```
pipeline/
  cli.py              fetch / prepare / apply stages, stage lock
  client.py           the only HTTP code: Paperless REST API v10 wrapper
  pdf_tools.py        blank-page detection, rotation check, split/merge/assemble, rendering
  form_fill.py        overlay text/checkmarks/a signature onto a blank scanned or flat form
  ocr.py              ocrmypdf wrapper and per-page text extraction
  selftest.py         live round-trip test of every write path (throwaway documents)
  check_connection.py smoke test for .env
docs/
  agents/             issue tracker, triage labels and domain-doc conventions for agent skills
  ingest-prompt.md    the prompt the runner scripts hand to the agent
  review-2026-09-22.md  (German) Paperless 3.1 capabilities vs. this pipeline, design rationale
AGENTS.md             agent instructions (generic)
LOCAL.md.example                template for your standing conventions (copy to LOCAL.md)
LOCAL_KNOWLEDGE.md.example      template for correspondents/patterns (copy to LOCAL_KNOWLEDGE.md)
LOCAL_LOG.md.example            template for the incident log (copy to LOCAL_LOG.md)
run_ingest.sh / .bat  unattended runner
work/                 scratch: inbox/, processed/, review/ (gitignored)
```

## License

MIT, see `LICENSE`.
