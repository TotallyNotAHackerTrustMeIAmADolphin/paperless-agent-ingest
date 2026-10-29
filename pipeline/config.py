import os

from dotenv import load_dotenv

load_dotenv()

PAPERLESS_URL = os.environ["PAPERLESS_URL"].rstrip("/")

# Name under which `python -m pipeline.store_token` keeps the token in the OS credential store
# (Windows Credential Manager, macOS Keychain, Secret Service). Lets the token stay out of
# `.env`, e.g. when the checkout lives in a synced cloud folder.
KEYRING_SERVICE = "paperless-agent-ingest"


def _token() -> str:
    token = os.environ.get("PAPERLESS_TOKEN")
    if token:
        return token
    try:
        import keyring
    except ImportError:
        keyring = None
    if keyring is not None:
        token = keyring.get_password(KEYRING_SERVICE, PAPERLESS_URL)
        if token:
            return token
    raise RuntimeError(
        "No Paperless token: set PAPERLESS_TOKEN in .env or store it with "
        "`python -m pipeline.store_token` (needs the keyring package)."
    )


PAPERLESS_TOKEN = _token()

# Tesseract language string for ocrmypdf, e.g. "deu+eng" or "eng". Every language listed must
# be installed as tessdata (see README).
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "deu+eng")
