"""check_figures.py on small hand-made documents: a clean figure, a caption value copied onto several records, a
figure no value cites, one explained by a gap, and figure evidence without a transcription.

    python -m pytest skills/curate-corpus/tests/test_check_figures.py
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_figures.py"


def record(rid, values, evidence, **extra):
    return {"id": rid, "props": {"evidence": evidence, **values, **extra}}


def fig_ev(eid, locator, **kw):
    return {"id": eid, "kind": "figure", "locator": locator, **kw}


def run(tmp_path, doc, figs):
    path = tmp_path / "doc.graph.json"
    path.write_text(json.dumps(doc))
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), str(path), figs], capture_output=True, text=True)
    return r.returncode, r.stdout


def test_clean(tmp_path):
    ev = [fig_ev("e1", "p. 5, Figure 3b", transcription="peak at 38.2°")]
    doc = {"nodes": [record("s1", {"xrd": {"value": 38.2, "unit": "deg", "variable": "v/two-theta",
                                            "evidence_ids": ["e1"]}}, ev)], "links": []}
    assert run(tmp_path, doc, "3") == (0, "ok: figure values clean\n")


def test_caption_value_copied(tmp_path):
    ev = [{"id": "c1", "kind": "text", "locator": "p. 5, Figure 3 caption", "quote": "at 300 °C"}]
    nodes = [record(f"r{i}", {"temperature": {"value": 300, "unit": "degC", "evidence_ids": ["c1"]}}, ev)
             for i in range(3)]
    code, out = run(tmp_path, {"nodes": nodes, "links": []}, "3")
    assert code == 1 and "copied onto 3 records" in out


def test_figure_not_cited(tmp_path):
    code, out = run(tmp_path, {"nodes": [], "links": []}, "4")
    assert code == 1 and "Figure 4: no value cites it yet" in out


def test_figure_explained_by_gap(tmp_path):
    ev = [fig_ev("g1", "p. 8, Figure 2a", transcription="only the 10 µm scale bar is printed")]
    gap = {"status": "not_reported", "missing_reason": "not_extractable", "evidence_ids": ["g1"]}
    noted = {"status": "not_reported", "note": "Fig. 5c: no y-axis numbers"}
    doc = {"nodes": [record("m1", {"fibre_diameter": gap, "height": noted}, ev)], "links": []}
    assert run(tmp_path, doc, "2,5")[0] == 0


def test_figure_evidence_without_transcription(tmp_path):
    ev = [fig_ev("e1", "p. 5, Fig. 3b")]
    doc = {"nodes": [record("s1", {"xrd": {"value": 38.2, "variable": "v/two-theta", "evidence_ids": ["e1"]}}, ev)],
           "links": []}
    code, out = run(tmp_path, doc, "3")
    assert code == 1 and "without a transcription" in out
