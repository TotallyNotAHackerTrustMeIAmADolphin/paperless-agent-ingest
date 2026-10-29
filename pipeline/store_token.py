"""Store the Paperless API token in the OS credential store instead of `.env`.

Run it yourself (the token is read with hidden input, never echoed):
    python -m pipeline.store_token

It saves the token under service "paperless-agent-ingest" and the instance's PAPERLESS_URL,
then tests it against the API. Afterwards remove the PAPERLESS_TOKEN line from `.env`;
`pipeline.config` falls back to the credential store when the variable is not set.
"""

import getpass

import keyring
import requests

from pipeline.config import KEYRING_SERVICE, PAPERLESS_URL


def main() -> None:
    token = getpass.getpass(f"Paperless API token for {PAPERLESS_URL} (hidden): ").strip()
    if not token:
        raise SystemExit("No token entered, nothing stored.")
    r = requests.get(
        f"{PAPERLESS_URL}/api/documents/?page_size=1",
        headers={"Authorization": f"Token {token}", "Accept": "application/json; version=10"},
        timeout=(10, 60),
    )
    if r.status_code != 200:
        raise SystemExit(f"Token rejected (HTTP {r.status_code}), nothing stored.")
    keyring.set_password(KEYRING_SERVICE, PAPERLESS_URL, token)
    print("Token works and is stored in the credential store.")
    print("Now remove the PAPERLESS_TOKEN line from .env.")


if __name__ == "__main__":
    main()
