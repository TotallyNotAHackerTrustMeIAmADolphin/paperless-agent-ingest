"""Thin wrapper around the Paperless-ngx REST API.

Docs: https://docs.paperless-ngx.com/api/ - written against Paperless-ngx 3.1 / API version 10
(see `API_VERSION`). Everything that talks HTTP to Paperless lives here.
"""
import hashlib
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline.config import PAPERLESS_TOKEN, PAPERLESS_URL

# Pinned so a server upgrade can't silently change field/endpoint semantics under us (Paperless
# keeps older API versions working for at least a year after a bump). Bump deliberately, after
# re-reading the "API Changelog" section of the docs and re-running `python -m pipeline.selftest`.
API_VERSION = 10

# (connect, read) seconds. Without a timeout a stalled server hangs apply() forever.
DEFAULT_TIMEOUT = (10, 300)


class _TimeoutAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().send(request, **kwargs)


session = requests.Session()
session.headers.update(
    {
        "Authorization": f"Token {PAPERLESS_TOKEN}",
        "Accept": f"application/json; version={API_VERSION}",
    }
)
_retry = Retry(
    total=6,
    backoff_factor=2,
    status_forcelist=[502, 503, 504],
    # POST is deliberately NOT retried: retrying document upload / entity-creation POSTs risks
    # silently creating a duplicate if the connection drops after Paperless already accepted the
    # request but before the response came back - the caller would see a clean single success
    # and never know. PATCH/DELETE/GET are safe to retry (re-applying the same fields, or
    # re-deleting/re-fetching an already-gone/present resource, is a no-op).
    allowed_methods=frozenset({"HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "PATCH"}),
)
_adapter = _TimeoutAdapter(max_retries=_retry)
session.mount("http://", _adapter)
session.mount("https://", _adapter)


def _url(path: str) -> str:
    return f"{PAPERLESS_URL}/api/{path.lstrip('/')}"


MAX_NAME_LEN = 128  # Paperless's title/tag/correspondent/document_type name fields are all
# CharField(max_length=128) - a longer value 400s the request rather than truncating
# server-side, which would otherwise abort an apply() batch partway through.


def _clip(value: str, field: str) -> str:
    if value and len(value) > MAX_NAME_LEN:
        clipped = value[:MAX_NAME_LEN]
        print(f"warning: {field} too long ({len(value)} chars), truncating to {MAX_NAME_LEN}: {value!r}")
        return clipped
    return value


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------ documents: read

def list_documents(**params):
    """Yield every document, following pagination. params are passed as query filters
    (e.g. is_in_inbox="true", tags__id__none=1, ordering="created", fields="id,title").

    Only use filter names that exist in `GET /api/schema/` - Paperless silently ignores unknown
    query params and then returns EVERY document (real case: `tags__is_inbox_tag` looked
    plausible, isn't a filter, and returned the whole archive). Use `fields=` to keep `content`
    (the full OCR text) out of listings you only need metadata from."""
    url = _url("documents/")
    while url:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        yield from data["results"]
        url = data["next"]
        params = None  # only needed on the first request; `next` already has them


def search_documents(query: str, page_size: int = 5) -> list[dict]:
    """Paperless's full-text search (same backend as the web UI's search box) - used as a
    duplicate-candidate heuristic, not as a general listing call."""
    resp = session.get(_url("documents/"), params={"query": query, "page_size": page_size})
    resp.raise_for_status()
    return resp.json()["results"]


def find_by_checksum(sha256: str) -> list[dict]:
    """Documents whose (latest-version) file has exactly these bytes. An empty list means an
    upload of that file won't be rejected as a duplicate; a hit is near-certain grounds for the
    duplicate-suspect tag (AGENTS.md) when it points at a *different* document than the one
    being replaced."""
    return list(list_documents(checksum__iexact=sha256, fields="id,title"))


def get_document(doc_id: int) -> dict:
    resp = session.get(_url(f"documents/{doc_id}/"))
    resp.raise_for_status()
    return resp.json()


def get_suggestions(doc_id: int) -> dict:
    """Paperless's own (non-LLM) classifier suggestions for a document:
    {"correspondents": [ids], "tags": [ids], "document_types": [ids], "storage_paths": [ids],
    "dates": ["YYYY-MM-DD", ...]}. Hints for whoever classifies, never a verdict."""
    resp = session.get(_url(f"documents/{doc_id}/suggestions/"))
    resp.raise_for_status()
    return resp.json()


def download_document(doc_id: int, dest: Path, original: bool = True) -> Path:
    """Download a document's file. original=True gets the as-uploaded file; original=False gets
    Paperless's PDF/A archive version - which documents pushed by this pipeline usually don't
    have (with the default `PAPERLESS_ARCHIVE_FILE_GENERATION=auto`, archive generation is
    skipped for files that already carry a text layer), in which case Paperless silently serves
    the original anyway."""
    params = {"original": "true"} if original else {}
    resp = session.get(_url(f"documents/{doc_id}/download/"), params=params, stream=True)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            f.write(chunk)
    return dest


# ------------------------------------------------------------------ documents: write

def upload_document(
    file_path: Path,
    title: str | None = None,
    tags: list[int] | None = None,
    correspondent: int | None = None,
    document_type: int | None = None,
    created=None,
) -> str:
    """POST a file for consumption as a NEW document. Returns the task UUID; ingestion happens
    asynchronously, so poll `get_task` to find the resulting document id. To replace the file of
    an existing document use `update_version` instead."""
    data = {}
    if title:
        data["title"] = _clip(title, "title")
    if tags:
        data["tags"] = tags
    if correspondent:
        data["correspondent"] = correspondent
    if document_type:
        data["document_type"] = document_type
    if created:
        data["created"] = created
    with open(file_path, "rb") as f:
        resp = session.post(
            _url("documents/post_document/"),
            data=data,
            files={"document": (Path(file_path).name, f)},
        )
    resp.raise_for_status()
    return resp.json()  # task UUID string


def update_version(doc_id: int, file_path: Path, label: str | None = None) -> str:
    """Replace a document's file in place (Paperless >= 3.0 "document versions"): the document
    keeps its id, title, tags, correspondent, document_type and custom fields; `content` and the
    served file come from the new version; the previous file stays retrievable under `versions`.
    Returns the task UUID - poll `get_task`; on SUCCESS `related_document` is the new *version's*
    id, which is not a standalone document (GET on it 404s, it's absent from listings). Verified
    live against Paperless 3.1.0; `pipeline.selftest` re-checks it."""
    data = {"version_label": label[:64]} if label else {}
    with open(file_path, "rb") as f:
        resp = session.post(
            _url(f"documents/{doc_id}/update_version/"),
            data=data,
            files={"document": (Path(file_path).name, f)},
        )
    resp.raise_for_status()
    return resp.json()  # task UUID string


def get_task(task_id: str) -> dict:
    """Returns the task dict with a normalized 'status' (upper-cased) and 'related_document'
    (single id or None), papering over a Paperless API version difference: older instances
    return a bare list with 'related_document'; newer ones return a paginated {"results": [...]}
    wrapper with lower-case status and 'related_document_ids' / 'result_data.document_id'.

    On FAILURE 'related_document' is always None: Paperless fills `related_document_ids` with
    the *existing* document a rejected duplicate upload collided with, which must never be
    mistaken for "the document this task created". The collision id is left where Paperless
    puts it, `result_data.duplicate_of`."""
    resp = session.get(_url("tasks/"), params={"task_id": task_id})
    resp.raise_for_status()
    data = resp.json()
    results = data["results"] if isinstance(data, dict) else data
    if not results:
        return {}
    task = results[0]
    task["status"] = task["status"].upper()
    if task["status"] == "FAILURE":
        task["related_document"] = None
    elif task.get("related_document") is None:
        related_ids = task.get("related_document_ids") or []
        task["related_document"] = (
            related_ids[0] if related_ids else (task.get("result_data") or {}).get("document_id")
        )
    return task


def update_document(doc_id: int, **fields) -> dict:
    """PATCH arbitrary fields, e.g. tags=[1,2], correspondent=3, title='...'. `tags` replaces
    the whole list (that is also how a document leaves the inbox: send the list without the
    inbox tag)."""
    if "title" in fields:
        fields["title"] = _clip(fields["title"], "title")
    resp = session.patch(_url(f"documents/{doc_id}/"), json=fields)
    resp.raise_for_status()
    return resp.json()


def delete_document(doc_id: int) -> None:
    """Moves the document (and all its versions) to Paperless's trash (30-day retention by
    default). Only call after the replacement is confirmed - see AGENTS.md."""
    resp = session.delete(_url(f"documents/{doc_id}/"))
    resp.raise_for_status()


# ------------------------------------------------------------------ tags / correspondents / types

def _list(kind: str) -> list[dict]:
    resp = session.get(_url(f"{kind}/"), params={"page_size": 1000})
    resp.raise_for_status()
    return resp.json()["results"]


def _get_or_create(kind: str, name: str) -> int:
    name = _clip(name, f"{kind[:-1]} name")
    for item in _list(kind):
        if item["name"].lower() == name.lower():
            return item["id"]
    # matching_algorithm=6 is Paperless's MATCH_AUTO ("Zuweisung automatisch erlernen") - new
    # entities must let the trained classifier suggest them on future documents; the API default
    # (1, "Any word") contributes no training signal at all. See AGENTS.md.
    resp = session.post(_url(f"{kind}/"), json={"name": name, "matching_algorithm": 6})
    resp.raise_for_status()
    return resp.json()["id"]


def list_tags() -> list[dict]:
    return _list("tags")


def get_or_create_tag(name: str) -> int:
    return _get_or_create("tags", name)


def list_correspondents() -> list[dict]:
    return _list("correspondents")


def get_or_create_correspondent(name: str) -> int:
    return _get_or_create("correspondents", name)


def list_document_types() -> list[dict]:
    return _list("document_types")


def get_or_create_document_type(name: str) -> int:
    return _get_or_create("document_types", name)


def inbox_tag_ids() -> set[int]:
    return {t["id"] for t in list_tags() if t.get("is_inbox_tag")}


def train_classifier() -> str:
    """Kick off Paperless's classifier retraining right now instead of waiting for its hourly
    schedule (superuser-only endpoint, works with the API token). Returns the task UUID."""
    resp = session.post(_url("tasks/run/"), json={"task_type": "train_classifier"})
    resp.raise_for_status()
    return resp.json()["task_id"]
