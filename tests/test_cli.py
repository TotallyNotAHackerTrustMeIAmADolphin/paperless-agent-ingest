from pathlib import Path

from pipeline import cli


def test_local_key_is_never_numeric_and_ascii():
    key = cli.local_key(Path("Prüfungsordnung Master (WiSe 26-27).pdf"), "20261019")
    assert key == "local-20261019-prufungsordnung-master-wise-26-27"
    assert not key.isdigit()


def test_local_key_falls_back_for_symbol_only_names():
    assert cli.local_key(Path("___.pdf"), "20261019") == "local-20261019-file"


def test_consume_skips_dotfiles_and_files_still_being_written(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setattr(cli, "CONSUME", tmp_path)
    old = time.time() - 60
    for name in (".gitkeep", "scan.pdf", "fresh.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
    os.utime(tmp_path / ".gitkeep", (old, old))
    os.utime(tmp_path / "scan.pdf", (old, old))
    assert [p.name for p in cli._consume_files()] == ["scan.pdf"]


def test_unsupported_type_is_reported_before_any_api_call(tmp_path):
    doc = tmp_path / "Antrag.docx"
    doc.write_bytes(b"PK")
    assert cli._stage_one(doc, "20261019") == ("unsupported", None)
