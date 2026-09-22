"""End-to-end check of every write path against the live Paperless instance, using throwaway
documents that are deleted again at the end (they land in Paperless's trash, auto-purged after
its retention period). Run after changing pipeline/client.py, after a Paperless upgrade, or
before trusting a fresh machine:

    python -m pipeline.selftest

Covers: upload + task polling, forced tag set (inbox tag removed), metadata PATCH, checksum
lookup, duplicate rejection surfacing as get_task FAILURE with result_data.duplicate_of and
related_document None, update_version keeping id/metadata and swapping content, download with
the pinned Accept header, delete. Creates its own throwaway tag and document type
("SELFTEST tag" / "SELFTEST type") and deletes them again at the end, so nothing of the real
taxonomy is touched.
"""
import sys
import tempfile
from pathlib import Path

import pymupdf

from pipeline import client

TITLE_PREFIX = "SELFTEST pipeline"
TAG_NAME = "SELFTEST tag"
TYPE_NAME = "SELFTEST type"


def _delete_entity(kind: str, entity_id: int) -> None:
    resp = client.session.delete(client._url(f"{kind}/{entity_id}/"))
    if resp.status_code != 404:
        resp.raise_for_status()


def _pdf(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text, fontsize=14)
    doc.save(path)
    doc.close()
    return path


def _wait(task_id: str) -> dict:
    import time

    for _ in range(80):
        task = client.get_task(task_id)
        if task.get("status") in ("SUCCESS", "FAILURE"):
            return task
        time.sleep(1.5)
    raise RuntimeError(f"task {task_id} did not finish")


def _apply_roundtrip(check, tag: int, doc_type: int) -> list[int]:
    """Drive cli.apply() against real throwaway documents with its work dirs redirected to a
    temp folder. Returns every Paperless id created (for cleanup), including ones apply()
    should have deleted (a second delete is harmless)."""
    import json

    from pipeline import cli

    ids: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cli.INBOX, cli.PROCESSED, cli.REVIEW = root / "inbox", root / "processed", root / "review"
        for p in (cli.INBOX, cli.PROCESSED, cli.REVIEW):
            p.mkdir()

        # Source 1: a normal fix-up -> must become a new version of the same document.
        src1 = _pdf(root, "src1.pdf", f"{TITLE_PREFIX} apply source 1")
        t = _wait(client.upload_document(src1, title=f"{TITLE_PREFIX} apply src1"))
        s1 = t["related_document"]
        ids.append(s1)
        (cli.INBOX / str(s1)).mkdir()
        (cli.INBOX / str(s1) / "original.pdf").write_bytes(src1.read_bytes())
        (cli.PROCESSED / str(s1)).mkdir()
        _pdf(cli.PROCESSED / str(s1), "ocr.pdf", f"{TITLE_PREFIX} apply source 1 FIXED")

        # Source 2: a "bundle" split into two parts -> two uploads, then the bundle is deleted.
        src2 = _pdf(root, "src2.pdf", f"{TITLE_PREFIX} apply source 2 (bundle)")
        t = _wait(client.upload_document(src2, title=f"{TITLE_PREFIX} apply src2"))
        s2 = t["related_document"]
        ids.append(s2)
        (cli.INBOX / str(s2)).mkdir()
        (cli.INBOX / str(s2) / "original.pdf").write_bytes(src2.read_bytes())
        for part in ("a", "b"):
            (cli.PROCESSED / f"{s2}{part}").mkdir()
            _pdf(cli.PROCESSED / f"{s2}{part}", "ocr.pdf", f"{TITLE_PREFIX} apply part {part}")

        classifications = [
            {"doc_id": s1, "title": f"{TITLE_PREFIX} versioned", "correspondent": None,
             "document_type": TYPE_NAME, "tags": [TAG_NAME], "created": "2026-02-03"},
            {"doc_id": f"{s2}a", "source_doc_id": s2, "title": f"{TITLE_PREFIX} part a",
             "correspondent": None, "document_type": TYPE_NAME, "tags": [TAG_NAME]},
            {"doc_id": f"{s2}b", "source_doc_id": s2, "title": f"{TITLE_PREFIX} part b",
             "correspondent": None, "document_type": TYPE_NAME, "tags": [TAG_NAME]},
        ]
        (cli.REVIEW / "classifications.json").write_text(json.dumps(classifications), encoding="utf-8")

        cli.apply()

        d1 = client.get_document(s1)
        check("version path: same id, new title, tags set, created set",
              d1["title"].endswith("versioned") and d1["tags"] == [tag] and str(d1["created"]).startswith("2026-02-03"))
        check("version path: content from fixed file, 2 versions", "FIXED" in (d1.get("content") or "") and len(d1["versions"]) == 2)
        check("version path: marker ocr.versioned.pdf", (cli.PROCESSED / str(s1) / "ocr.versioned.pdf").exists())
        parts = list(client.list_documents(fields="id,title,tags", text=f"{TITLE_PREFIX} part"))
        ids.extend(p["id"] for p in parts)
        check("split path: two new documents with forced tags", len(parts) == 2 and all(p["tags"] == [tag] for p in parts), str([(p['id'], p['tags']) for p in parts]))
        check("split path: bundle deleted", client.session.get(client._url(f"documents/{s2}/")).status_code == 404)
        check("split path: markers ocr.uploaded.pdf", all((cli.PROCESSED / f"{s2}{p}" / "ocr.uploaded.pdf").exists() for p in "ab"))
    return ids


def main() -> int:
    failures = []

    def check(label: str, ok: bool, detail=""):
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{f' ({detail})' if detail else ''}")
        if not ok:
            failures.append(label)

    r = client.session.get(client._url("documents/"), params={"page_size": 1, "fields": "id"})
    print(f"server {client.PAPERLESS_URL}: Paperless {r.headers.get('x-version')}, API v{r.headers.get('x-api-version')}")
    check("API version pinned and accepted", r.headers.get("x-api-version") == str(client.API_VERSION))

    tag = client.get_or_create_tag(TAG_NAME)
    doc_type = client.get_or_create_document_type(TYPE_NAME)
    inbox = client.inbox_tag_ids()
    created_ids: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        a = _pdf(folder, "a.pdf", f"{TITLE_PREFIX} A")
        b = _pdf(folder, "b.pdf", f"{TITLE_PREFIX} B (new version)")

        print("upload")
        check("no document has these bytes yet", client.find_by_checksum(client.sha256_of(a)) == [])
        task = _wait(client.upload_document(a, title=f"{TITLE_PREFIX} A", tags=[tag], document_type=doc_type, created="2026-01-02"))
        doc_id = task.get("related_document")
        check("upload task SUCCESS with document id", task.get("status") == "SUCCESS" and doc_id is not None, str(task.get("status")))
        if doc_id is None:
            print("cannot continue without a document")
            return 1
        created_ids.append(doc_id)
        doc = client.get_document(doc_id)
        if set(doc["tags"]) != {tag}:
            client.update_document(doc_id, tags=[tag])
            doc = client.get_document(doc_id)
        check("tags forced to exactly the requested set (inbox tag gone)", set(doc["tags"]) == {tag} and not (set(doc["tags"]) & inbox))
        check("document_type and created applied", doc["document_type"] == doc_type and str(doc["created"]).startswith("2026-01-02"))
        check("content extracted", "SELFTEST" in (doc.get("content") or ""))
        check("checksum lookup finds it", [h["id"] for h in client.find_by_checksum(client.sha256_of(a))] == [doc_id])

        print("patch")
        client.update_document(doc_id, title=f"{TITLE_PREFIX} A (renamed)", created="2026-01-03")
        doc = client.get_document(doc_id)
        check("PATCH title/created", doc["title"].endswith("(renamed)") and str(doc["created"]).startswith("2026-01-03"))

        print("duplicate rejection")
        task = _wait(client.upload_document(a, title=f"{TITLE_PREFIX} A (dup)"))
        dup_of = (task.get("result_data") or {}).get("duplicate_of")
        check("identical bytes -> FAILURE with duplicate_of", task.get("status") == "FAILURE" and dup_of == doc_id, f"status={task.get('status')} duplicate_of={dup_of}")
        check("related_document is None on FAILURE", task.get("related_document") is None)
        if task.get("status") == "SUCCESS" and task.get("related_document"):
            created_ids.append(task["related_document"])  # instance consumed duplicates - clean up

        print("update_version")
        before = client.get_document(doc_id)
        task = _wait(client.update_version(doc_id, b, label="selftest v2"))
        check("version task SUCCESS", task.get("status") == "SUCCESS", str(task.get("status")))
        after = client.get_document(doc_id)
        check("same id, title/tags/type kept", after["id"] == doc_id and after["title"] == before["title"] and after["tags"] == before["tags"] and after["document_type"] == before["document_type"])
        check("content comes from the new version", "new version" in (after.get("content") or ""))
        check("two versions listed, root marked", len(after["versions"]) == 2 and any(v["is_root"] for v in after["versions"]))
        version_ids = [v["id"] for v in after["versions"] if v["id"] != doc_id]
        gone = all(client.session.get(client._url(f"documents/{v}/")).status_code == 404 for v in version_ids)
        check("version id is not a standalone document", gone)

        print("download")
        dl = client.download_document(doc_id, folder / "dl.pdf")
        check("download works with the Accept header", dl.stat().st_size > 0 and dl.read_bytes()[:4] == b"%PDF")

    print("apply() end-to-end (version path + split path, in a temp work dir)")
    try:
        created_ids.extend(_apply_roundtrip(check, tag, doc_type))
    except Exception as e:  # noqa: BLE001 - report, then still clean up
        check("apply roundtrip ran without exception", False, repr(e))

    print("cleanup")
    for i in created_ids:
        if client.session.get(client._url(f"documents/{i}/")).status_code == 404:
            continue  # apply() already deleted it (split bundle)
        client.delete_document(i)
    leftovers = [d["id"] for d in client.list_documents(fields="id,title", text=TITLE_PREFIX)]
    check("no selftest documents left", leftovers == [], str(leftovers))
    _delete_entity("tags", tag)
    _delete_entity("document_types", doc_type)
    check("selftest tag and type removed",
          not any(t["name"] == TAG_NAME for t in client.list_tags())
          and not any(t["name"] == TYPE_NAME for t in client.list_document_types()))
    inbox_count = sum(1 for _ in client.list_documents(is_in_inbox="true", fields="id"))
    print(f"inbox now holds {inbox_count} document(s)")

    print("\nPASS" if not failures else f"\nFAIL: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
