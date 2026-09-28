"""Read-mostly helper commands used during classification. Run as
`python -m pipeline.cli <command>`; none of them take the stage lock because none writes to work/.

  report                    one line per document from work/review/classification_report.json
  compare NEW_ID OLD_ID     is the new document's text the same as an already-filed document?
  mark-duplicate NEW OLD    tag and retitle the already-filed side of a duplicate pair

See docs/playbook.md for when to use which.
"""
import difflib
import json
import re
from pathlib import Path

from pipeline import client

REPORT = Path("work/review/classification_report.json")
PROCESSED = Path("work/processed")
DUPLICATE_TAG = "Duplikat-Verdacht"
SUFFIX_RE = re.compile(r"\s+-\s+Duplikat-Verdacht \(vgl\. Dok\. \d+\)\s*$")

# At or above this word-level similarity two texts count as the same document. Identical letters
# re-rendered by a portal score 0.95-1.0 (text extraction noise); a yearly follow-up letter with other dates scores lower
# because the dates and numbers differ, which is why the verdict is a hint, not an action.
SAME_THRESHOLD = 0.95
SIMILAR_THRESHOLD = 0.85


def normalize(text: str) -> list[str]:
    """Words of `text` without the `--- Page N of M ---` markers, whitespace collapsed."""
    text = re.sub(r"^--- Page \d+ of \d+ ---$", " ", text, flags=re.M)
    return text.split()


def similarity(a: str, b: str) -> float:
    wa, wb = normalize(a), normalize(b)
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def verdict(score: float) -> str:
    if score >= SAME_THRESHOLD:
        return "SAME"
    if score >= SIMILAR_THRESHOLD:
        return "SIMILAR (read both: rescan, other copy or follow-up letter)"
    return "DIFFERENT"


def with_duplicate_suffix(title: str, other_id: int) -> str:
    """Title with exactly one ` - Duplikat-Verdacht (vgl. Dok. N)` suffix, replacing an old one."""
    return SUFFIX_RE.sub("", title) + f" - {DUPLICATE_TAG} (vgl. Dok. {other_id})"


def report() -> None:
    if not REPORT.exists():
        raise SystemExit(f"{REPORT} missing - run `prepare` first")
    for d in json.loads(REPORT.read_text(encoding="utf-8")):
        flags = []
        if d.get("dropped_pages"):
            flags.append(f"dropped {d['dropped_pages']}")
        if d.get("rotated_pages"):
            flags.append(f"rotated {d['rotated_pages']}")
        if d.get("low_confidence_pages"):
            flags.append(f"lowconf {d['low_confidence_pages']}")
        if d.get("exact_duplicate_of"):
            flags.append(f"EXACT DUP of {d['exact_duplicate_of']}")
        if d.get("possible_duplicates"):
            flags.append("maybe dup of " + ",".join(str(p["id"]) for p in d["possible_duplicates"]))
        s = d.get("suggestions", {})
        print(f"{d['doc_id']}: {d['pages_before']}->{d['pages_kept']} pages  guess {d.get('created_guess')}")
        print(f"    title: {d.get('original_title')}")
        print(f"    paperless suggests: {s.get('correspondents')} {s.get('document_types')} {s.get('dates')}")
        if flags:
            print(f"    flags: {'; '.join(flags)}")


def compare(new_id: int, old_id: int) -> None:
    path = PROCESSED / str(new_id) / "content.md"
    if not path.exists():
        raise SystemExit(f"{path} missing - run `prepare` first")
    new_text = path.read_text(encoding="utf-8")
    old = client.get_document(old_id)
    score = similarity(new_text, old.get("content", ""))
    print(f"{new_id} vs {old_id} ({old['title']!r}): similarity {score:.3f} -> {verdict(score)}")


def mark_duplicate(new_id: int, old_id: int) -> None:
    """Tag + retitle the already-filed side. The new side is handled through its entry in
    classifications.json (tag `Duplikat-Verdacht`, same title suffix)."""
    old = client.get_document(old_id)
    tag_id = client.get_or_create_tag(DUPLICATE_TAG)
    tags = sorted(set(old["tags"]) | {tag_id})
    title = with_duplicate_suffix(old["title"], new_id)
    if title == old["title"] and tags == sorted(old["tags"]):
        print(f"{old_id}: already marked, nothing to do")
    else:
        client.update_document(old_id, title=title, tags=tags)
        print(f"{old_id}: retitled {title!r}, tags {tags}")
    print(f"for the new document use suffix: {with_duplicate_suffix('', old_id).lstrip()}")


COMMANDS = {"report": (report, 0), "compare": (compare, 2), "mark-duplicate": (mark_duplicate, 2)}

