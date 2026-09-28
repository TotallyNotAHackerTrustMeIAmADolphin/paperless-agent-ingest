from pipeline import tools


def test_suffix_added_once_and_replaced():
    t = tools.with_duplicate_suffix("Rechnung (X) - 01.01.2026", 5)
    assert t == "Rechnung (X) - 01.01.2026 - Duplikat-Verdacht (vgl. Dok. 5)"
    assert tools.with_duplicate_suffix(t, 9).endswith("(vgl. Dok. 9)")
    assert tools.with_duplicate_suffix(t, 9).count("Duplikat-Verdacht") == 1


def test_similarity_ignores_page_markers_and_whitespace():
    a = "--- Page 1 of 1 ---\nHallo   Welt\nzweite Zeile"
    b = "Hallo Welt zweite Zeile"
    assert tools.similarity(a, b) == 1.0
    assert tools.verdict(1.0) == "SAME"


def test_different_texts_score_low():
    assert tools.verdict(tools.similarity("eins zwei drei vier", "fuenf sechs sieben acht")) == "DIFFERENT"
