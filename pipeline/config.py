import os

from dotenv import load_dotenv

load_dotenv()

PAPERLESS_URL = os.environ["PAPERLESS_URL"].rstrip("/")
PAPERLESS_TOKEN = os.environ["PAPERLESS_TOKEN"]

# Tesseract language string for ocrmypdf, e.g. "deu+eng" or "eng". Every language listed must
# be installed as tessdata (see README).
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "deu+eng")
