import os
import socket
from functools import cache
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Entry in the OS credential store (Windows Credential Manager, macOS Keychain, Secret Service).
# Store it with `python -m pipeline.set_token`.
KEYRING_SERVICE = "paperless-pipeline"
KEYRING_USER = "api-token"

# Tesseract language string for ocrmypdf, e.g. "deu+eng" or "eng". Every language listed must
# be installed as tessdata (see README).
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "deu+eng")


def _reachable(url: str, timeout: float = 1.5) -> bool:
    u = urlparse(url)
    try:
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout).close()
        return True
    except OSError:
        return False


@cache
def resolve_url() -> tuple[str, str]:
    """(base url, why). `PAPERLESS_URL` in the environment wins (preflight sets it so child
    processes skip the probe); else `PAPERLESS_URL_LOCAL` if its port answers, else
    `PAPERLESS_URL_REMOTE`."""
    if os.environ.get("PAPERLESS_URL"):
        return os.environ["PAPERLESS_URL"].rstrip("/"), "PAPERLESS_URL override"
    local = os.environ.get("PAPERLESS_URL_LOCAL", "").rstrip("/")
    remote = os.environ.get("PAPERLESS_URL_REMOTE", "").rstrip("/")
    if local and _reachable(local):
        return local, "local"
    if remote:
        return remote, "remote (local unreachable)" if local else "remote"
    if local:
        return local, "local (unreachable, no remote configured)"
    raise RuntimeError("set PAPERLESS_URL_LOCAL and/or PAPERLESS_URL_REMOTE in .env")


@cache
def get_token() -> str:
    """`PAPERLESS_TOKEN` in the environment (headless/CI) wins, else the OS keyring."""
    token = os.environ.get("PAPERLESS_TOKEN")
    if not token:
        import keyring

        token = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if not token:
        raise RuntimeError("no API token: run `python -m pipeline.set_token`")
    return token


def __getattr__(name):  # lazy, so set_token can import the constants before a token exists
    if name == "PAPERLESS_URL":
        return resolve_url()[0]
    if name == "PAPERLESS_TOKEN":
        return get_token()
    raise AttributeError(name)
