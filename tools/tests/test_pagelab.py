"""The graph explorer's private graphs (tools/pagelab.py, run in the browser by site/worker.js): any source added in the
page by the code of ``scale add-source``, kept as a folder its download carries, whose manifest rebuilds the graph on
the command line byte for byte. Offline: the tables are the public synthetic examples of the built-in mappings."""

import base64
import json
import re
import shlex
import shutil
from pathlib import Path

import pytest

from conftest import EXAMPLES, REPO
from scale import curate, pagelab
from scale.cli import main
from scale.util import ScaleError, load_doc

X = EXAMPLES / "scale-catal/data-mapping-examples"
PROJECT, LAB = "lab:project/co2-demo", "lab:org/demo-lab"
WHEN = "2026-03-02T10:00:00Z"
ANSWERS = {
    "co2rr-fe-wide.xlsx": {"recorded_at": WHEN, "confirm": ["assumptions:co2rr-faradaic-efficiency"]},
    "thermal-tests.csv": {"recorded_at": WHEN, "confirm": ["assumptions:thermal-catalytic-test"]},
    "kinetics.csv": {"recorded_at": WHEN, "confirm": ["assumptions:kinetics-rates"]},
    "dft-energies.csv": {"confirm": ["assumptions:dft-adsorption-energy"]},
    "characterization.csv": {"recorded_at": WHEN, "confirm": ["assumptions:characterization-summary", "unit:Ni/Al (XPS)",
                                                              "unit:O/Ni (XPS)"]},
}


@pytest.fixture(autouse=True)
def _outside(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def test_a_table_is_read_asked_about_and_added_as_the_command_adds_it(tmp_path):
    base = tmp_path / "lab"
    made = pagelab.new(base, "g1", PROJECT, LAB, "project", "CO2RR lab data", repo=REPO)
    assert made["stewardship"]["access"] == "project" and (base / "g1/graph.json").is_file()
    st = pagelab.stage(base, "g1", "co2rr-fe-wide.xlsx", b64(X / "co2rr-fe-wide.xlsx"))
    assert st["file"] == "sources/co2rr-fe-wide.xlsx" and st["detected"]["route"] == "table" and len(st["sha256"]) == 64
    # first the questions, nothing written; the view's material: each role's column and unit, the wide columns, the
    # header, the sheets, the other kinds of table
    q = pagelab.table(base, "g1", st["file"], repo=REPO)
    before = (base / "g1/graph.json").read_bytes()
    assert q["status"] == "questions" and {x["id"] for x in q["questions"]} == {"recorded_at", "assumptions:co2rr-faradaic-efficiency"}
    assert q["roles"]["potential"] == {"column": "E (V vs RHE)", "unit": "V vs RHE", "unit_from_header": True, "score": 1.0, "by": "name"}
    assert [c["column"] for c in q["melt_columns"]["fe"]] == ["FE CO (%)", "FE H2 (%)", "FE HCOO- (%)"]
    assert [r["sheet"] for r in q["reading"]] == ["Notes", "FE", "GC calibration"] and "FE total (%)" in q["unmapped_columns"]
    assert q["template_roles"]["potential"]["required"] and {t["id"] for t in q["library"]} >= {"kinetics-rates"}
    assert q["command"].startswith("scale add-source sources/co2rr-fe-wide.xlsx --into graph.json")
    # a binding the reader changes is an option of the command, on the sheet it was read from (the page pins it)
    rebound = pagelab.table(base, "g1", st["file"], {"sheet": "FE", "bind": {"sample": "Run"}}, repo=REPO)
    assert rebound["roles"]["sample"]["by"] == "you" and "--sheet FE" in rebound["command"] and "--bind sample=Run" in rebound["command"]
    # answered: a dry run says what would be added, then the addition, recorded in the manifest
    dry = pagelab.table(base, "g1", st["file"], ANSWERS["co2rr-fe-wide.xlsx"], repo=REPO)
    assert (dry["status"], dry["records"], dry["results"], dry["verify"]["status"]) == ("dry_run", 3, 12, "verified")
    assert (base / "g1/graph.json").read_bytes() == before
    w = pagelab.table(base, "g1", st["file"], ANSWERS["co2rr-fe-wide.xlsx"], write=True, repo=REPO)
    assert w["status"] == "added" and w["mapping"] == "work/mappings/co2rr-fe-wide.co2rr-faradaic-efficiency.map.yaml"
    man = json.loads((base / "g1/manifest.json").read_text())
    step = man["steps"][-1]
    assert step["step"] == "table" and step["sha256"] == st["sha256"] and step["read"]["template"] == "co2rr-faradaic-efficiency"
    assert man["graph"]["sha256"] == pagelab.sha256((base / "g1/graph.json").read_bytes())
    assert man["schema"]["version"] and len(man["schema"]["spec_sha256"]) == 64 and man["tool"].startswith("scale-catal ")
    assert pagelab.verify(base, "g1")["datasets"][0]["status"] == "verified"


def test_the_download_rebuilds_the_graph_on_the_command_line_byte_for_byte(tmp_path, monkeypatch):
    base = tmp_path / "lab"
    pagelab.new(base, "g1", PROJECT, LAB, "project", "CO2RR lab data", repo=REPO)
    for name, opts in ANSWERS.items():
        st = pagelab.stage(base, "g1", name, b64(X / name))
        assert pagelab.table(base, "g1", st["file"], opts, write=True, repo=REPO)["status"] == "added", name
    files = pagelab.files(base, "g1")
    assert {"graph.json", "manifest.json", "rights.yaml"} <= set(files) and sum(k.startswith("work/mappings/") for k in files) == 5
    man = json.loads(base64.b64decode(files["manifest.json"]))
    # in an empty folder with sources/ copied, the manifest's commands write the same graph and mappings
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    shutil.copytree(base / "g1/sources", rebuilt / "sources")
    monkeypatch.chdir(rebuilt)
    for cmd in man["reproduce"]["commands"]:
        args = shlex.split(cmd)
        assert args[0] == "scale"
        assert main(["--repo", str(REPO), *args[1:]]) in (0, None), cmd
    assert (rebuilt / "graph.json").read_bytes() == (base / "g1/graph.json").read_bytes()
    for m in (base / "g1/work/mappings").iterdir():
        assert (rebuilt / "work/mappings" / m.name).read_bytes() == m.read_bytes()
    # the working copy comes back whole, and nothing is written outside its folder
    pagelab.restore(base, "g2", files)
    assert (base / "g2/graph.json").read_bytes() == (base / "g1/graph.json").read_bytes()
    for bad in ("../evil.txt", "sources/..\\..\\evil.txt", "C:\\evil.txt", "/etc/evil", "a//b"):
        with pytest.raises(ScaleError, match="not a path inside"):
            pagelab.restore(base, "g3", {**files, bad: base64.b64encode(b"x").decode()})
    with pytest.raises(ScaleError, match="id"):
        pagelab.new(base, "../g", PROJECT, LAB, repo=REPO)


def test_what_a_file_is_is_read_by_the_router_and_names_stay_inside_sources(tmp_path):
    base = tmp_path / "lab"
    assert pagelab.detect(base, "t.csv", b64(X / "kinetics.csv"))["detected"] == {"route": "table", "format": "csv"}
    assert pagelab.detect(base, "g.json", b64(X / "seed.json"))["detected"]["route"] == "graph"
    assert pagelab.detect(base, "p.pdf", base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode())["detected"]["route"] == "document"
    assert pagelab.detect(base, text="https://doi.org/10.1021/ABC.1")["detected"] == {"route": "document", "doi": "10.1021/abc.1", "format": "doi"}
    with pytest.raises(ScaleError, match="old.xls"):
        pagelab.detect(base, "old.xls", base64.b64encode(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest").decode())
    assert not (base / ".inbox").exists()
    pagelab.new(base, "g1", PROJECT, LAB, repo=REPO)
    st = pagelab.stage(base, "g1", "../../up/kinetics.csv", b64(X / "kinetics.csv"))
    assert st["file"] == "sources/kinetics.csv"
    other = pagelab.stage(base, "g1", "kinetics.csv", base64.b64encode(b"run,x\n1,2\n3,4\n5,6\n").decode())
    assert other["file"].startswith("sources/kinetics-") and other["file"] != st["file"]
    with pytest.raises(ScaleError, match="access"):
        pagelab.new(base, "g2", PROJECT, LAB, "everyone", repo=REPO)


def test_a_curated_publication_is_merged_and_a_table_links_to_it(tmp_path):
    base = tmp_path / "lab"
    pagelab.new(base, "g1", PROJECT, LAB, "project", "Paper and its data", repo=REPO)
    txt = tmp_path / "paper.txt"
    txt.write_text("[page 1]\nCO2 electroreduction on copper (a synthetic example text). Table 1 lists the DFT energies "
                   "of each adsorbate on Cu(111) and Cu(100), computed with one functional.\n")
    curate.start(tmp_path / "folder", text=txt, repo=REPO)
    doc = load_doc(next((tmp_path / "folder").glob("*.json")) if not (tmp_path / "folder/graph.json").is_file()
                   else tmp_path / "folder/graph.json")
    rights_text = (tmp_path / "folder/rights.yaml").read_text() if (tmp_path / "folder/rights.yaml").is_file() else "sources: {}\n"
    res = pagelab.publication(base, "g1", "paper", doc, rights_text, {"file": "paper.txt", "model": "test"}, repo=REPO)
    assert res["status"] == "merged" and re.fullmatch(r"scale add-source sources/paper-[0-9a-f]{10}/graph\.json --into graph\.json --merge", res["command"])
    # a second curation of a file of the same name never replaces the first's (the manifest's step still replays)
    other = {**doc, "graph": {**doc["graph"], "title": "another reading"}}
    again = pagelab.publication(base, "g1", "paper", other, rights_text, {}, write=False, repo=REPO)
    assert again["command"] != res["command"] and (base / "g1" / res["command"].split()[2]).is_file()
    src = next(s["id"] for s in pagelab.sources_of(base, "g1"))
    st = pagelab.stage(base, "g1", "dft-energies.csv", b64(X / "dft-energies.csv"))
    opts = {**ANSWERS["dft-energies.csv"], "from_source": src, "locator": "Table 1", "data_rights": "in_copyright"}
    w = pagelab.table(base, "g1", st["file"], opts, write=True, repo=REPO)
    assert w["status"] == "added" and w["linked_to_source"] and f"--from-source {src}" in w["command"]
    g = load_doc(base / "g1/graph.json")
    assert any(e["type"] == "REPORTED_IN" and e["source"] == w["dataset"] and e["target"] == src for e in g["links"])
    man = json.loads((base / "g1/manifest.json").read_text())
    assert [s["step"] for s in man["steps"]] == ["new", "graph", "table"] and man["steps"][1]["publication"]["curated"]["file"] == "paper.txt"
    assert not any(p.suffix in (".txt", ".pdf") for p in (base / "g1").rglob("*"))     # the source's text is never kept


def test_without_processes_the_framework_is_still_found_by_its_git_folder(monkeypatch, tmp_path):
    """Pyodide runs no process (no git): the framework repository is then found by its .git, and a folder outside it
    is no framework."""
    from scale import datarepo

    def no_processes(*a, **k):
        raise OSError(138, "emscripten does not support processes")
    monkeypatch.setattr(datarepo.subprocess, "run", no_processes)
    assert datarepo.framework_root(REPO / "tools" / "pagelab.py") is not None
    assert datarepo.framework_root(tmp_path) is None
    with pytest.raises(ScaleError, match="framework repository"):
        pagelab.new(REPO / "work-test-lab", "g1", PROJECT, LAB, repo=REPO)
    assert not (REPO / "work-test-lab").exists()
