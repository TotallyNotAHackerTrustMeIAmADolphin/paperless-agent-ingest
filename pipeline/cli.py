"""Orchestrates the pipeline stages. Run as `python -m pipeline.cli <stage>`.

Stages:
  fetch    - download every untouched Paperless document (still carrying an inbox tag, or - as a
             fallback for pre-inbox-tag legacy uploads - having neither correspondent nor
             document_type) into work/inbox/<id>/original.pdf, next to a meta.json snapshot of
             its Paperless metadata. Already-classified documents are skipped, so re-running the
             pipeline is safe.
  prepare  - for each downloaded doc: if it isn't already a born-digital PDF (real text layer),
             drop blank pages, fix rotation, and OCR - that scan-artifact cleanup is skipped
             entirely for born-digital PDFs, which are never rotated or padded with blank scan
             backs. Extracts the text per page (with "--- Page N of M ---" markers) for
             classification. Writes work/processed/<id>/fixed.pdf + ocr.pdf + content.md and a
             combined report to work/review/classification_report.json for a human (or an
             agent) to read and turn into work/review/classifications.json
  apply    - read work/review/classifications.json and push the result back: the fixed file
             becomes a new *version* of the same Paperless document (id stays, old file stays
             retrievable), then the metadata is PATCHed. Only split parts (several entries
             replacing one bundled scan) are uploaded as new documents, with the bundle deleted
             once every part succeeded. See apply() for the exact rules.
"""
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from PIL import Image

from pipeline import client, ocr, pdf_tools

INBOX = Path("work/inbox")
PROCESSED = Path("work/processed")
REVIEW = Path("work/review")

# Paperless can consume a raw image directly (e.g. a phone scanning app that uploads a JPEG
# instead of assembling a PDF first) - `mime_type` values whose bytes need converting to a
# single-page PDF before anything in `pdf_tools`/`ocr` (which only ever handle PDFs) can touch
# them. See the `fetch()` conversion step below.
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/tiff", "image/bmp", "image/gif", "image/webp"}

# Metadata kept per document in work/inbox/<id>/meta.json (no `content`: it's large and prepare
# re-extracts the text from the fixed file anyway).
META_FIELDS = "id,title,created,added,tags,correspondent,document_type,mime_type,page_count,original_file_name,versions"

VERSION_LABEL = "ingest-pipeline"


def _untouched_documents():
    """Yield every document the pipeline still has to classify, once each.

    The primary signal is Paperless's own inbox tag (`is_in_inbox=true`): it's applied on every
    fresh consumption and only cleared when apply() PATCHes the document with an explicit tag
    list. Correspondent/document_type presence is NOT a usable "already classified" signal -
    Paperless's auto-matcher (matching_algorithm=6) assigns those on ingest too (real cases:
    docs 139/153 on 2026-09-02, doc 275 on 2026-09-10). The second query is a fallback for
    legacy uploads from before the inbox tag existed: no inbox tag, but nothing assigned either.
    """
    seen = set()
    for doc in client.list_documents(is_in_inbox="true", fields=META_FIELDS):
        seen.add(doc["id"])
        yield doc
    for doc in client.list_documents(
        correspondent__isnull="true", document_type__isnull="true", fields=META_FIELDS
    ):
        if doc["id"] not in seen:
            seen.add(doc["id"])
            yield doc


def fetch():
    n = 0
    for d in _untouched_documents():
        n += 1
        folder = INBOX / str(d["id"])
        dest = folder / "original.pdf"
        folder.mkdir(parents=True, exist_ok=True)
        # Always refresh the snapshot: prepare() reads it instead of calling the API again, and
        # it survives the document being replaced/deleted in Paperless in the meantime.
        (folder / "meta.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        if dest.exists():
            print(f"exists  {d['id']}")
            continue
        try:
            client.download_document(d["id"], dest)
            if d.get("mime_type") in IMAGE_MIME_TYPES:
                # Downloaded bytes are e.g. a JPEG saved under a .pdf name - pikepdf/pdf2image
                # would fail on it later (real case: doc 261, a raw
                # "scan_2026_09_02T18_16_30.jpeg" upload, crashed prepare() with "unable to find
                # trailer dictionary"). Convert in place so original.pdf is always a real PDF.
                Image.open(dest).convert("RGB").save(dest, "PDF")
        except requests.exceptions.HTTPError as e:
            print(f"skip {d['id']}: download failed ({e})")
            continue
        print(f"fetched {d['id']} ({d['title']})")
    print(f"{n} untouched document(s) in Paperless")


def _duplicate_candidates(content: str, own_id=None) -> list[dict]:
    """Cheap duplicate/partial-rescan hint: pull a handful of the longer, less-generic words
    out of the extracted text and run them through Paperless's own full-text search. Only
    surfaces documents that are already classified (real correspondent/document_type) - other
    still-unclassified inbox items would just be noise. This is a hint for a human (or an agent)
    to go read, never a basis for automatic action - see the duplicate-suspect convention in
    AGENTS.md."""
    import re

    words = re.findall(r"[A-Za-zÄÖÜäöüß]{6,}", content)
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
        if len(seen) >= 10:
            break
    if len(seen) < 4:
        return []
    try:
        hits = client.search_documents(" ".join(seen))
    except requests.exceptions.HTTPError:
        return []
    return [
        {"id": h["id"], "title": h["title"]}
        for h in hits
        # The document itself is in the index too and, thanks to the auto-matcher, usually
        # already has a correspondent/document_type - never report it as its own duplicate.
        if (h.get("correspondent") or h.get("document_type")) and h["id"] != own_id
    ]


def _load_meta(doc_dir: Path, doc_id):
    """Metadata snapshot for the report: meta.json written by fetch(), else a live lookup
    (None if the doc is gone). This is NOT the untouched check - the snapshot is stale as soon
    as apply() has run (a folder whose meta.json still carries the inbox tag got re-OCR'd on
    every later prepare, doc 391 on 2026-09-22); prepare() decides against the live set."""
    meta_path = doc_dir / "meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    try:
        return client.get_document(doc_id)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def _named_suggestions(doc_id: int, names: dict[str, dict[int, str]]) -> dict:
    """Paperless's classifier suggestions with ids resolved to names - a hint for the
    classification step (it's the same model whose auto-assignments the inbox check exists to
    distrust, so treat it as one opinion, not ground truth)."""
    try:
        raw = client.get_suggestions(doc_id)
    except requests.exceptions.HTTPError:
        return {}
    return {
        "correspondents": [names["correspondents"].get(i, i) for i in raw.get("correspondents", [])],
        "document_types": [names["document_types"].get(i, i) for i in raw.get("document_types", [])],
        "tags": [names["tags"].get(i, i) for i in raw.get("tags", [])],
        "dates": raw.get("dates", []),
    }


def prepare():
    report = []
    # The one source of truth for "still to do": the same live query fetch() uses. work/inbox/
    # keeps every folder ever fetched (originals are never deleted), so anything not in this
    # set has been applied, deleted or classified since and must be skipped.
    live_untouched = {d["id"] for d in _untouched_documents()}
    names = {
        "correspondents": {c["id"]: c["name"] for c in client.list_correspondents()},
        "document_types": {t["id"]: t["name"] for t in client.list_document_types()},
        "tags": {t["id"]: t["name"] for t in client.list_tags()},
    }
    doc_dirs = sorted(
        (p for p in INBOX.iterdir() if p.is_dir()),
        key=lambda p: (0, int(p.name)) if p.name.isdigit() else (1, p.name),
    )
    for doc_dir in doc_dirs:
        name = doc_dir.name
        is_paperless = name.isdigit()
        doc_id = int(name) if is_paperless else name
        original = doc_dir / "original.pdf"
        if not original.exists():
            continue

        meta = None
        if is_paperless:
            if doc_id not in live_untouched:
                print(f"skip {doc_id}: not untouched in Paperless any more (already processed)")
                continue
            meta = _load_meta(doc_dir, doc_id)
            if meta is None:
                print(f"skip {doc_id}: no longer in Paperless (already processed)")
                continue

        # Everything that touches the file stays inside this try: one bad document (corrupt/
        # encrypted PDF, an all-blank scan the BLANK_INK_THRESHOLD heuristic can't explain, ...)
        # must not discard the report entries already built for the documents before it.
        try:
            original_pages = pdf_tools.page_count(original)
            fixed_out = PROCESSED / str(doc_id) / "fixed.pdf"
            ocr_out = PROCESSED / str(doc_id) / "ocr.pdf"
            content_out = PROCESSED / str(doc_id) / "content.md"

            if ocr.needs_ocr(original):
                analysis = pdf_tools.analyze_pdf(original)
                pages_kept = pdf_tools.fix_pdf(original, fixed_out, analysis)
            else:
                # Born-digital PDF (already has a real text layer): blank-page/rotation
                # detection exists for physical-scan artifacts and doesn't apply here, so skip
                # the per-page render+OSD pass entirely - it's pure overhead on documents like
                # this.
                analysis = []
                fixed_out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(original, fixed_out)
                pages_kept = original_pages

            ocr.run_ocr(fixed_out, ocr_out)
            content = ocr.extract_text(ocr_out)
            content_out.write_text(content, encoding="utf-8")

            low_conf = [a["page"] for a in analysis if a["low_confidence"]]
            dup_candidates = _duplicate_candidates(content, own_id=doc_id)
            exact_dups = [
                h for h in client.find_by_checksum(client.sha256_of(original)) if h["id"] != doc_id
            ]
            suggestions = _named_suggestions(doc_id, names) if is_paperless else {}
        except Exception as e:
            print(f"FAILED preparing {doc_id}: {e}")
            continue

        report.append(
            {
                "doc_id": doc_id,
                "original_title": meta["title"] if meta else name,
                # Paperless's own date guess - any date-shaped string in the text can fool it
                # (birthdates, cited law dates). Check against the document's printed date.
                "created_guess": meta["created"] if meta else None,
                "pages_before": original_pages,
                "pages_kept": pages_kept,
                # Page numbers below are 0-indexed against the ORIGINAL file; content.md and
                # ocr.pdf are numbered after the blank-page drops.
                "dropped_pages": [a["page"] for a in analysis if a["blank"]],
                "rotated_pages": [a["page"] for a in analysis if a["rotation"]],
                "low_confidence_pages": low_conf,
                "exact_duplicate_of": exact_dups,
                "possible_duplicates": dup_candidates,
                "suggestions": suggestions,
                "text_excerpt": content[:2000],
            }
        )
        notes = []
        if low_conf:
            notes.append(
                f"LOW OCR CONFIDENCE on original page(s) {low_conf} - render and check "
                "visually before trusting the extracted text for these"
            )
        if exact_dups:
            notes.append(f"BYTE-IDENTICAL to {exact_dups} - duplicate suspect")
        if dup_candidates:
            notes.append(f"possible duplicate of {dup_candidates} - read both before deciding")
        suffix = f" ({'; '.join(notes)})" if notes else ""
        print(f"prepared {doc_id}: {original_pages} -> {pages_kept} pages{suffix}")

    REVIEW.mkdir(parents=True, exist_ok=True)
    report_path = REVIEW / "classification_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {report_path} - read it and write work/review/classifications.json")


def _normalize_sources(entry: dict) -> list:
    doc_id = entry["doc_id"]
    default_sources = [int(doc_id)] if str(doc_id).isdigit() else []
    raw = entry.get("source_doc_id", default_sources)
    sources = raw if isinstance(raw, list) else [raw]
    # Normalize digit-strings to int so an explicit "source_doc_id": "87" in a hand-written
    # classifications.json lands on the same dict key as the implicit int default, instead
    # of silently tracking the same Paperless id under two different keys.
    return [int(s) if isinstance(s, str) and s.isdigit() else s for s in sources if s is not None]


def _resolve_fields(entry: dict) -> dict:
    fields = {
        "title": entry["title"],
        "tags": [client.get_or_create_tag(name) for name in entry.get("tags", [])],
        "correspondent": (
            client.get_or_create_correspondent(entry["correspondent"]) if entry.get("correspondent") else None
        ),
        "document_type": (
            client.get_or_create_document_type(entry["document_type"]) if entry.get("document_type") else None
        ),
    }
    if entry.get("created"):
        fields["created"] = entry["created"]
    return fields


def apply():
    """Each classification entry's `doc_id` addresses its processed folder
    (work/processed/<doc_id>/ocr.pdf); it need not be a real Paperless id (e.g. a split-part
    key like "64a"). An optional `source_doc_id` names the original Paperless document(s) this
    entry replaces - a single id, a list of ids (several separately uploaded originals merged
    into one entry, e.g. "87m" replacing 87 and 89), or omitted to default to `doc_id` when that
    is numeric. Several entries may also share one source id when a bundled scan was split.

    How the result reaches Paperless depends on that shape:

    * One entry, one source (the normal fix-up: rotated, blank pages dropped, OCR'd): the fixed
      file is pushed as a new VERSION of the source document (`client.update_version`) and the
      metadata PATCHed onto it. The id stays, the previous file stays under `versions`, nothing
      is deleted. Marker on success: `ocr.versioned.pdf`.
    * One entry, one source, and `ocr.pdf` is byte-identical to the source's `original.pdf`
      (born-digital, nothing to fix): no file is pushed at all - Paperless would reject the
      identical bytes as a duplicate - the metadata is PATCHed directly. Marker: `ocr.patched.pdf`.
    * One entry, several sources (a merge): new version of the FIRST source + PATCH, the
      remaining sources are deleted once that succeeded. Marker: `ocr.versioned.pdf`.
    * Several entries sharing one source (a split bundle): each part is uploaded as a new
      document (`client.upload_document`), Paperless's own consumption-time tags are forced back
      to the entry's list, and the bundle is deleted only after EVERY part succeeded. Marker:
      `ocr.uploaded.pdf`.

    Markers make a rerun with the same classifications.json (e.g. after fixing whatever made a
    different entry fail) skip finished entries instead of pushing them again; a versioned or
    patched source is by definition the final document and is never deleted, on any run."""
    classifications = json.loads((REVIEW / "classifications.json").read_text(encoding="utf-8"))
    entries = [(entry, _normalize_sources(entry)) for entry in classifications]
    refs = Counter(s for _, sources in entries for s in sources)

    source_results: dict[int, list[bool]] = {}
    final_sources: set[int] = set()  # versioned/patched in place -> never delete

    for entry, sources in entries:
        doc_id = entry["doc_id"]
        folder = PROCESSED / str(doc_id)
        ocr_pdf = folder / "ocr.pdf"
        uploaded_marker = folder / "ocr.uploaded.pdf"
        patched_marker = folder / "ocr.patched.pdf"
        versioned_marker = folder / "ocr.versioned.pdf"
        primary = sources[0] if sources and isinstance(sources[0], int) else None
        is_split = primary is not None and refs[primary] > 1

        if not ocr_pdf.exists():
            if patched_marker.exists() or versioned_marker.exists():
                print(f"skip {doc_id}: already applied in place in a previous apply() run")
                success = True
                final_sources.add(primary)
            elif uploaded_marker.exists():
                print(f"skip {doc_id}: already uploaded in a previous apply() run")
                success = True
            else:
                print(f"skip {doc_id}: no processed OCR pdf found")
                success = False
            for s in sources:
                source_results.setdefault(s, []).append(success)
            continue

        try:
            fields = _resolve_fields(entry)

            if is_split or primary is None:
                task_id = client.upload_document(ocr_pdf, **fields)
                new_doc_id = _wait_for_task(task_id)
                success = new_doc_id is not None
                if success:
                    # Paperless's own consumption pipeline applies tags beyond what we asked
                    # for - inbox tags (e.g. "NEW") on every newly consumed document, plus
                    # whatever its matching_algorithm=6 classifier predicts (which can misfire).
                    # Classification here is done by reading the text, so force the tag set
                    # back to exactly what the entry says.
                    uploaded = client.get_document(new_doc_id)
                    if set(uploaded["tags"]) != set(fields["tags"]):
                        client.update_document(new_doc_id, tags=fields["tags"])
                    ocr_pdf.rename(uploaded_marker)
                    print(f"uploaded {doc_id} -> new document {new_doc_id} ('{entry['title']}')")
                else:
                    print(f"FAILED upload for {doc_id}, task {task_id} - not deleting source")
            else:
                source_original = INBOX / str(primary) / "original.pdf"
                if source_original.exists() and client.sha256_of(source_original) == client.sha256_of(ocr_pdf):
                    client.update_document(primary, **fields)
                    ocr_pdf.rename(patched_marker)
                    success = True
                    print(f"patched {doc_id}: file unchanged, updated document {primary} in place ('{entry['title']}')")
                else:
                    task_id = client.update_version(primary, ocr_pdf, label=VERSION_LABEL)
                    success = _wait_for_task(task_id) is not None
                    if success:
                        client.update_document(primary, **fields)
                        ocr_pdf.rename(versioned_marker)
                        extra = f", merged from {sources[1:]}" if len(sources) > 1 else ""
                        print(f"versioned {doc_id}: new file version on document {primary}{extra} ('{entry['title']}')")
                    else:
                        print(f"FAILED new version for {doc_id} on document {primary}, task {task_id}")
                final_sources.add(primary)
        except requests.exceptions.HTTPError as e:
            # Isolate one entry's failure (bad field, transient API error, ...) so it doesn't
            # abort the whole batch - which would otherwise skip the deletion pass below even
            # for entries that already succeeded earlier in this same run.
            print(f"FAILED entry {doc_id}: {e} - not deleting source, continuing")
            success = False

        for s in sources:
            source_results.setdefault(s, []).append(success)

    for source_id, results in source_results.items():
        if source_id in final_sources:
            print(f"keeping {source_id}: it IS the final document (versioned/patched in place)")
        elif all(results):
            client.delete_document(source_id)
            print(f"deleted original Paperless document {source_id} ({len(results)} part(s) replaced it)")
        else:
            print(f"NOT deleting original {source_id}: {results.count(False)}/{len(results)} part(s) failed")


def _wait_for_task(task_id: str, timeout_s: int = 180):
    """Poll until the consumption task finishes. Returns the related document id on SUCCESS
    (for an upload: the new document; for update_version: the new version's id), else None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        task = client.get_task(task_id)
        status = task.get("status")
        if status == "SUCCESS":
            related = task.get("related_document")
            return int(related) if related is not None else None
        if status == "FAILURE":
            detail = task.get("result_data") or task.get("status_display")
            print(f"task {task_id} failed: {detail}")
            return None
        time.sleep(2)
    print(f"task {task_id} timed out waiting for completion")
    return None


STAGES = {"fetch": fetch, "prepare": prepare, "apply": apply}

LOCK_FILE = Path("work/.stage.lock")
LOCK_STALE_S = 3 * 3600


def _acquire_lock(stage: str) -> None:
    """One stage at a time. `run_ingest.bat` (possibly scheduled) and an interactive session can
    both run the pipeline against the same work/ directory; two `prepare`s or `apply`s racing on
    work/review/*.json silently overwrite each other (real case 2026-08-27). This doesn't stop the
    two *sessions* from interleaving stages - it stops the same stage running twice at once,
    which is the destructive case. A lock older than LOCK_STALE_S is treated as left behind by a
    crashed run and taken over."""
    import os

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"{stage} pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            return
        except FileExistsError:
            age = time.time() - LOCK_FILE.stat().st_mtime
            holder = LOCK_FILE.read_text(errors="replace").strip()
            if age > LOCK_STALE_S and attempt == 0:
                print(f"taking over stale lock ({age / 3600:.1f} h old): {holder}")
                LOCK_FILE.unlink(missing_ok=True)
                continue
            sys.exit(
                f"another pipeline stage is running ({holder}, {age / 60:.0f} min ago) - wait for it, "
                f"or delete {LOCK_FILE} if that run is dead"
            )


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STAGES:
        print(f"usage: python -m pipeline.cli <{'|'.join(STAGES)}>")
        sys.exit(1)
    _acquire_lock(sys.argv[1])
    try:
        STAGES[sys.argv[1]]()
    finally:
        LOCK_FILE.unlink(missing_ok=True)
