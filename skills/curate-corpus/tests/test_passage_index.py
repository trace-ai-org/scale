"""passage_index.py: a source folder's page texts become passages that are verbatim substrings of their pages; adding
the folder again replaces them; search finds a term within the given sources and reports a source with no hit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import passage_index as P  # noqa: E402

TEXT = ("[page 1]\nTITLE: Cu-Au nanoparticles\n\nThe particles were annealed at 400 °C under H2 for 2 h.\n\n"
        + "Filler about catalysis. " * 120 + "\n\n[page 2]\nThe gauge pressure was 20 bar.\n\n"
        "[page S1]\nSupporting information file: si.docx\nCalcined at 500 °C in air.\n\n")


def folder(tmp_path, name, text):
    f = tmp_path / name
    (f / "work" / "texts").mkdir(parents=True)
    (f / "work" / "texts" / f"{name}.txt").write_text(text, encoding="utf-8")
    return f


def test_passages_are_verbatim_and_located(tmp_path):
    db = P.connect(tmp_path / "p.db")
    out = P.add(db, folder(tmp_path, "10.1000-abc", TEXT))
    assert out["pages"] == 3 and out["passages"] >= 3
    pages = dict(P.pages_of(TEXT))
    for page, s, e, text in db.execute("select page, char_start, char_end, text from passages"):
        assert pages[page][s:e] == text
    assert {r[0] for r in db.execute("select page from passages")} == {"1", "2", "S1"}


def test_adding_again_replaces_and_search_reports_misses(tmp_path):
    db = P.connect(tmp_path / "p.db")
    f = folder(tmp_path, "10.1000-abc", TEXT)
    P.add(db, f)
    n = db.execute("select count(*) from passages").fetchone()[0]
    P.add(db, f)
    assert db.execute("select count(*) from passages").fetchone()[0] == n
    P.add(db, folder(tmp_path, "10.1000-other", "[page 1]\nNothing relevant here.\n"))
    res = P.search(db, tmp_path / "p.db", ["10.1000-abc", "10.1000-other"], ["annealed", '"gauge pressure"'])
    assert {(h["source_id"], h["page"]) for h in res["hits"]} == {("10.1000-abc", "1"), ("10.1000-abc", "2")}
    assert res["none_found_in_passages"] == ["10.1000-other"]
    assert P.add(db, tmp_path / "empty") == {"source_id": "empty", "left_out": "no page texts in work/texts"}
