"""Manual smoke test: confirms PAPERLESS_URL / PAPERLESS_TOKEN in .env actually work.

Usage: python -m pipeline.check_connection
"""
from pipeline.client import list_documents

if __name__ == "__main__":
    docs = list(list_documents(page_size=1))
    print(f"OK - connected, sample document: {docs[0]['title'] if docs else '(none found)'}")
