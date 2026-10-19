from pathlib import Path

from pipeline import cli


def test_local_key_is_never_numeric_and_ascii():
    key = cli.local_key(Path("Prüfungsordnung Master (WiSe 26-27).pdf"), "20261019")
    assert key == "local-20261019-prufungsordnung-master-wise-26-27"
    assert not key.isdigit()


def test_local_key_falls_back_for_symbol_only_names():
    assert cli.local_key(Path("___.pdf"), "20261019") == "local-20261019-file"
