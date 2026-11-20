"""Store the Paperless API token in the OS keyring (Windows Credential Manager).

Usage: python -m pipeline.set_token             (prompts, input hidden)
       python -m pipeline.set_token --from-env  (moves PAPERLESS_TOKEN out of .env, deletes that line)
"""
import getpass
import re
import sys
from pathlib import Path

import keyring

from pipeline.config import KEYRING_SERVICE, KEYRING_USER

ENV = Path(".env")
LINE = re.compile(r"^PAPERLESS_TOKEN=(.*)\r?\n?", re.M)

if __name__ == "__main__":
    if "--from-env" in sys.argv:
        text = ENV.read_text(encoding="utf-8")
        m = LINE.search(text)
        token = m.group(1).strip() if m else ""
        if not token:
            sys.exit("no PAPERLESS_TOKEN value in .env")
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
        if keyring.get_password(KEYRING_SERVICE, KEYRING_USER) != token:
            sys.exit("keyring write did not read back, .env left untouched")
        ENV.write_text(LINE.sub("", text, count=1), encoding="utf-8")
    else:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, getpass.getpass("Paperless API token: ").strip())
    print("token stored in the OS keyring")
