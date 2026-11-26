"""Any source into a graph by one entry point (scale add-source; tools/addsource.py), the starter document
(scale data new, scale data init's graphs/lab.json) and the data repository's default stewardship in the writers of
new documents (scale call template, scale source start). Offline: a DOI is answered by a fake fetch, a publication's
text is a text file, and the tables are the public synthetic examples of the built-in mappings."""

import json
import shutil
import subprocess

import pytest

from conftest import EXAMPLES, REPO
from scale import addsource, audience, curate, datarepo, datatable, pgraph, rights
from scale.api import ScaleToolkit
from scale.cli import main
from scale.egress import EgressGate
from scale.util import ScaleError, load_doc, load_yaml, write_canonical

X = EXAMPLES / "scale-catal/data-mapping-examples"
PROJECT = "lab:project/co2"
CANARY = "CANARY-5d1e77-private-run-of-batch-K9"
TEXT = ("[page 1]\nCO2 methanation over nickel (a synthetic example text).\nThe CO2 conversion was 46.2 % at 300 C and "
        "10 bar over Ni-Al2O3, and CH4 was the main product in every run of the series.\n\n[page 2]\nTable 2 lists "
        "the runs; their data are in the supporting file.\n")


@pytest.fixture(autouse=True)
def _git_alone(tmp_path, monkeypatch):
    """git reads no user or system configuration (a global core.hooksPath would take the hooks) and commits under a
    fixed identity; the working directory is outside this repository."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@example.org\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("SCALE_CONTACT_EMAIL", raising=False)
    monkeypatch.chdir(tmp_path)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r


@pytest.fixture()
def lab(tmp_path):
    """A data repository with its starter document."""
    d = tmp_path / "lab"
    datarepo.init(d, project=PROJECT, contributor="lab:org/ni-group")
    return d


CONFIRMED = [f"assumptions:{t}" for t in ("co2rr-faradaic-efficiency", "thermal-catalytic-test", "kinetics-rates",
                                            "dft-adsorption-energy", "characterization-summary")]


def add(source, into, **kw):
    """The router, with the mappings' assumptions confirmed (a test of the question itself passes confirm=[])."""
    kw.setdefault("confirm", CONFIRMED)
    return addsource.add(str(source), into, repo=REPO, **kw)


def spec_of(doc):
    return pgraph.load_spec(pgraph.spec_for(REPO, doc["graph"]["schema_version"], None, doc))


def valid(path):
    doc = load_doc(path)
    return pgraph.validate(doc, spec_of(doc)).errors == []


def curate_one_claim(folder, quote="conversion was 46.2 %"):
    """Stand-in for the agent's reading (the curate-corpus skill): one evidenced Claim."""
    info = load_doc(folder / "source.json")
    g = folder / f"{info['slug']}.graph.json"
    doc = load_doc(g)
    doc["nodes"].append({"id": f"ex:pub/{info['slug']}/claim/1", "label": "Claim", "props": {
        "name": "Methanation conversion", "statement": "Ni-Al2O3 converts about half of the CO2 at 300 C.",
        "claim_kind": "finding", "claim_status": "proposed", "origin": "reported",
        "evidence": [{"source": info["source_id"], "locator": "p. 1", "kind": "text", "quote": quote}]}})
    write_canonical(g, doc)
    assert curate.check(folder, REPO, derive_links=True)["ok"]
    return info["source_id"]


# ------------------------------------------------------------------------------------------ the starter document
def test_data_init_writes_a_valid_starter_with_the_policys_stewardship(lab):
    doc = load_doc(lab / "graphs/lab.json")
    st = doc["graph"]["stewardship"]
    assert st == {"access": "project", "project": PROJECT, "contributed_by": ["lab:org/ni-group"]}
    assert doc["graph"]["data_status"] == "research_data" and doc["graph"]["schema_version"] == spec_of(doc)["version"]
    assert {(n["id"], n["label"]) for n in doc["nodes"]} == {("lab:org/ni-group", "Institution"), (PROJECT, "Project")}
    assert valid(lab / "graphs/lab.json")
    git(lab, "add", ".")
    assert git(lab, "commit", "-qm", "init", check=False).returncode == 0          # the guard admits it


def test_data_new_takes_the_repository_defaults_and_refuses_what_it_cannot_state(lab, tmp_path):
    res = datarepo.new_document(lab / "graphs/second.json", title="Second", repo=REPO)
    assert res["stewardship"]["project"] == PROJECT and res["data_repository"] == str(lab) and valid(res["written"])
    with pytest.raises(ScaleError, match="exists"):
        datarepo.new_document(lab / "graphs/second.json", repo=REPO)
    with pytest.raises(ScaleError, match="its project: give --project"):
        datarepo.new_document(tmp_path / "loose.json", contributor="lab:org/x", repo=REPO)
    out = datarepo.new_document(tmp_path / "loose.json", project="lab:project/p", contributor="lab:org/x",
                                access="institution", repo=REPO)
    assert out["data_repository"] is None and valid(out["written"])
    # in another project's data repository a document of fewer readers is refused (the hooks would refuse it)
    with pytest.raises(ScaleError, match="fewer readers"):
        datarepo.new_document(lab / "graphs/narrow.json", access="institution", repo=REPO)
    assert main(["--repo", str(REPO), "data", "new", str(lab / "graphs/third.json")]) == 0


def test_nothing_of_a_person_is_written_into_the_framework_repository(tmp_path):
    with pytest.raises(ScaleError, match="SCALE framework repository"):
        datarepo.new_document(REPO / "graphs" / "mine.json", project="lab:project/p", contributor="lab:org/x", repo=REPO)
    assert not (REPO / "graphs" / "mine.json").exists()
    with pytest.raises(ScaleError, match="SCALE framework repository"):
        add(X / "kinetics.csv", REPO / "domain/examples/scale-catal/data-mapping-examples/seed.json")
    tk = ScaleToolkit(workspace=tmp_path / "ws", repo=REPO)
    out = tk.call("scale_add_source", {"source": str(X / "kinetics.csv"), "into": str(X / "seed.json")})
    assert "SCALE framework repository" in out["error"]
    out = tk.call("scale_data_new", {"file": str(tmp_path / "elsewhere" / "d.json"), "project": "lab:project/p",
                                     "contributor": "lab:org/x"})
    assert "only in the working directory" in out["error"]


def test_template_and_source_start_honour_the_data_repository_defaults(lab, tmp_path, monkeypatch):
    t = ScaleToolkit(workspace=lab / "work", repo=REPO).call("scale_template", {})
    st = t["document"]["graph"]["stewardship"]
    assert st["access"] == "project" and st["project"] == PROJECT and st["contributed_by"] == ["lab:org/ni-group"]
    assert t["valid"] and t["stewardship_from"] == str(lab / "scale-data.yaml")
    outside = ScaleToolkit(workspace=tmp_path / "ws", repo=REPO).call("scale_template", {})
    assert outside["document"]["graph"]["stewardship"]["access"] == "public" and "stewardship_from" not in outside
    (tmp_path / "paper.txt").write_text(TEXT, encoding="utf-8")
    res = curate.start(lab / "work/sources/p", text=tmp_path / "paper.txt", repo=REPO)
    g = load_doc(lab / "work/sources/p" / res["graph"])
    assert g["graph"]["stewardship"] == {"contributed_by": ["lab:org/ni-group"], "project": PROJECT, "access": "project"}
    assert valid(lab / "work/sources/p" / res["graph"])
    elsewhere = curate.start(tmp_path / "w", text=tmp_path / "paper.txt", repo=REPO)
    assert load_doc(tmp_path / "w" / elsewhere["graph"])["graph"]["stewardship"]["access"] == "public"
    # scale source start run inside a data repository puts its folder in the repository's work/
    monkeypatch.chdir(lab)
    assert main(["--repo", str(REPO), "source", "start", "--text", str(tmp_path / "paper.txt")]) == 0
    assert (lab / "work/sources/paper/paper.graph.json").is_file()


# ------------------------------------------------------------------------------------------ what a source is
def test_the_kind_is_read_from_the_bytes_and_the_name(tmp_path):
    (tmp_path / "paper.txt").write_text(TEXT)
    (tmp_path / "runs.txt").write_text("run\tT (K)\trate\nA\t500\t1.2\nB\t520\t1.9\nC\t540\t2.4\n")
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.7 a file")
    (tmp_path / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
    (tmp_path / "notes.docx").write_bytes(b"not read")
    (tmp_path / "g.json").write_text("{}")
    assert addsource.detect(str(tmp_path / "paper.txt"))["route"] == "document"
    assert addsource.detect(str(tmp_path / "runs.txt"))["route"] == "table"
    assert addsource.detect(str(tmp_path / "scan.pdf"))["format"] == "pdf"
    assert addsource.detect(str(X / "co2rr-fe-wide.xlsx"))["format"] == "xlsx"
    assert addsource.detect(str(X / "kinetics.csv"))["route"] == "table"
    assert addsource.detect(str(tmp_path / "g.json"))["route"] == "graph"
    assert addsource.detect("https://doi.org/10.9999/Example.1") == {"route": "document", "doi": "10.9999/example.1",
                                                                     "format": "doi"}
    assert addsource.detect(str(tmp_path / "paper.txt"), "table")["route"] == "table"
    for name, why in (("old.xls", "save it as .xlsx"), ("notes.docx", "export it as PDF")):
        with pytest.raises(ScaleError, match=why):
            addsource.detect(str(tmp_path / name))
    with pytest.raises(ScaleError, match="no such file, and not a DOI"):
        addsource.detect("missing.csv")


# ------------------------------------------------------------------------------------------ tables
def test_a_csv_table_is_imported_verified_and_committed_when_nothing_is_uncertain(lab):
    doc_path = lab / "graphs/lab.json"
    before = doc_path.read_bytes()
    dry = add(X / "kinetics.csv", doc_path, recorded_at="2026-03-02T10:00:00Z", dry_run=True)
    assert dry["status"] == "dry_run" and dry["would_add"] and doc_path.read_bytes() == before
    assert not (lab / "raw/kinetics.csv").exists()
    res = add(X / "kinetics.csv", doc_path, recorded_at="2026-03-02T10:00:00Z")
    assert res["status"] == "added" and res["template"] == "kinetics-rates" and res["records"] == 3
    assert res["verify"]["status"] == "verified" and res["validation"]["valid"]
    assert res["raw_copy"] == str(lab / "raw/kinetics.csv") and (lab / "raw/kinetics.csv").is_file()
    assert res["checklist"]["catalysis"]["missing"] > 0          # what is still to record, reported
    doc = load_doc(doc_path)
    assert valid(doc_path) and datatable.verify(doc, base=doc_path.parent)["status"] == "verified"
    ds = next(n for n in doc["nodes"] if n["id"] == res["dataset"])
    assert ds["props"]["uri"] == "../raw/kinetics.csv"
    # the Dataset is a source the cells are quoted from: its rights entry is written, so the guard admits the commit
    assert rights.load_rights(lab / "rights.yaml")[res["dataset"]]["class"] == "open"
    assert (lab / "work/mappings").is_dir() and res["mapping"].endswith(".map.yaml")
    git(lab, "add", ".")
    r = git(lab, "commit", "-qm", "data", check=False)
    assert r.returncode == 0, r.stderr
    assert "raw/kinetics.csv" not in git(lab, "ls-files").stdout
    again = add(X / "kinetics.csv", doc_path, recorded_at="2026-03-02T10:00:00Z")
    assert again["status"] == "unchanged" and again["added"] == 0
    assert main(["--repo", str(REPO), "data", "verify", str(doc_path), "--require-all"]) == 0


def test_an_xlsx_table_asks_first_and_imports_once_answered(lab, capsys):
    doc_path = lab / "graphs/lab.json"
    before = doc_path.read_bytes()
    q = add(X / "co2rr-fe-wide.xlsx", doc_path, confirm=[])
    assert q["status"] == "questions" and q["written"] is None and doc_path.read_bytes() == before
    assert [x["id"] for x in q["questions"]] == ["recorded_at", "assumptions:co2rr-faradaic-efficiency"]
    assert not (lab / "raw").exists() or not any((lab / "raw").iterdir())         # nothing copied either
    assert "--recorded-at YYYY-MM-DDThh:mm:ssZ" in q["retry"] and q["sheet"] == "FE"
    assert q["retry_tool"]["arguments"]["recorded_at"] == "YYYY-MM-DDThh:mm:ssZ"
    assert any("header row was found" in n for n in q["notes"]) and q["assumptions"]
    # the command line asks back with exit 1 and writes nothing
    assert main(["--repo", str(REPO), "add-source", str(X / "co2rr-fe-wide.xlsx"), "--into", str(doc_path)]) == 1
    out = capsys.readouterr().out
    assert "QUESTIONS" in out and "QUESTION recorded_at" in out and "then: scale add-source" in out
    res = add(X / "co2rr-fe-wide.xlsx", doc_path, recorded_at="2026-03-02T10:00:00Z")
    assert res["status"] == "added" and res["file_dataset"] and res["verify"]["status"] == "verified"
    assert valid(doc_path)


def test_an_uncertain_match_returns_questions_and_never_a_guess(lab, tmp_path):
    doc_path = lab / "graphs/lab.json"
    before = doc_path.read_bytes()
    (tmp_path / "t.csv").write_text("run,Temp 1 (°C),Temp 2 (°C),X CO2 (%)\nA,250,260,10\nB,300,310,20\n")
    q = add(tmp_path / "t.csv", doc_path, origin="computed", template="thermal-catalytic-test")
    amb = next(x for x in q["questions"] if x["id"] == "bind:temperature")
    assert amb["candidates"] == ["Temp 1 (°C)", "Temp 2 (°C)"] and amb["settle"]["tool_arguments"]["bind"]
    assert q["status"] == "questions" and doc_path.read_bytes() == before
    (tmp_path / "u.csv").write_text("run id,Reactor temp,X CO2 (%)\nA,500,10\nB,520,12\n")
    q = add(tmp_path / "u.csv", doc_path, origin="computed", template="thermal-catalytic-test")
    ids = {x["id"] for x in q["questions"]}
    assert "bind:temperature" in ids and "unit:Reactor temp" in ids          # a similar name; a unit assumed
    res = add(tmp_path / "u.csv", doc_path, origin="computed", template="thermal-catalytic-test",
              bind={"temperature": "Reactor temp"}, confirm=["unit:Reactor temp", *CONFIRMED])
    assert res["status"] == "added", res.get("questions")
    # a publication's table asks for its rights
    q = add(tmp_path / "t.csv", doc_path, origin="reported", template="thermal-catalytic-test",
            bind={"temperature": "Temp 2 (°C)"})
    assert [x["id"] for x in q["questions"]] == ["rights"]


# ------------------------------------------------------------------------------------------ publications
def test_a_publication_starts_a_curation_folder_and_finish_folds_it_in(lab, tmp_path):
    doc_path = lab / "graphs/lab.json"
    (tmp_path / "paper.txt").write_text(TEXT, encoding="utf-8")
    res = add(tmp_path / "paper.txt", doc_path, source_kind="thesis")
    folder = lab / "work/sources/paper"
    assert res["status"] == "started" and res["folder"] == str(folder) and res["pages"] == 2
    assert any("--finish" in n for n in res["next"]) and any("curate-corpus" in n for n in res["next"])
    g = load_doc(folder / "paper.graph.json")
    assert g["graph"]["stewardship"] == load_doc(doc_path)["graph"]["stewardship"]
    assert next(n for n in g["nodes"] if n["label"] == "Source")["props"]["kind"] == "thesis"
    assert add(tmp_path / "paper.txt", doc_path)["already"]
    sid = curate_one_claim(folder)
    out = addsource.add(None, doc_path, repo=REPO, finish_folder=folder)
    assert out["status"] == "merged" and sid in out["added_ids"] and out["rights"]["added"] == [sid]
    doc = load_doc(doc_path)
    assert valid(doc_path) and any(n["id"] == sid for n in doc["nodes"])
    assert rights.load_rights(lab / "rights.yaml")[sid]["class"] == "in_copyright"
    assert addsource.add(None, doc_path, repo=REPO, finish_folder=folder)["status"] == "unchanged"
    # the publication's table: the Dataset is REPORTED_IN the publication; a retry changes nothing
    t = add(X / "kinetics.csv", doc_path, origin="reported", data_rights="in_copyright", from_source=sid,
            locator="Table 2")
    assert t["status"] == "added" and any(x.endswith("/reported-in/paper") for x in t["linked_to_source"])
    doc = load_doc(doc_path)
    assert any(e["type"] == "REPORTED_IN" and e["source"] == t["dataset"] and e["target"] == sid for e in doc["links"])
    assert add(X / "kinetics.csv", doc_path, origin="reported", data_rights="in_copyright", from_source=sid,
               locator="Table 2")["status"] == "unchanged"
    with pytest.raises(ScaleError, match="holds no Source"):
        add(X / "dft-energies.csv", doc_path, origin="computed", from_source="ex:pub/source/none")
    git(lab, "add", ".")
    r = git(lab, "commit", "-qm", "paper and its table", check=False)
    assert r.returncode == 0, r.stderr


def test_finish_refuses_a_graph_that_does_not_check(lab, tmp_path):
    (tmp_path / "paper.txt").write_text(TEXT, encoding="utf-8")
    add(tmp_path / "paper.txt", lab / "graphs/lab.json")
    folder = lab / "work/sources/paper"
    g = load_doc(folder / "paper.graph.json")
    g["nodes"].append({"id": "ex:pub/paper/claim/x", "label": "Claim", "props": {"name": "x", "origin": "reported",
                       "evidence": [{"source": "ex:pub/source/paper", "locator": "p. 1", "kind": "text",
                                     "quote": "a sentence the paper does not hold"}]}})
    write_canonical(folder / "paper.graph.json", g)
    before = (lab / "graphs/lab.json").read_bytes()
    with pytest.raises(ScaleError, match="fix them first"):
        addsource.add(None, lab / "graphs/lab.json", repo=REPO, finish_folder=folder)
    assert (lab / "graphs/lab.json").read_bytes() == before


def test_a_doi_without_an_open_copy_asks_for_the_pdf(lab):
    crossref = {"message": {"title": ["A closed paper"], "issued": {"date-parts": [[2025]]}, "type": "journal-article",
                            "URL": "https://doi.org/10.9999/closed.1"}}

    def get(url, headers):
        if url.startswith(curate.CROSSREF):
            return json.dumps(crossref).encode()
        return json.dumps({"best_oa_location": None}).encode()
    res = addsource.add("10.9999/closed.1", lab / "graphs/lab.json", repo=REPO, get=get, email="x@example.org")
    assert res["status"] == "questions" and res["questions"][0]["id"] == "file"
    assert "--doi 10.9999/closed.1" in res["retry"] and not (lab / "work/sources").exists()


# ------------------------------------------------------------------------------------------ graph files
def test_a_graph_file_is_validated_and_merged_only_when_asked_and_an_id_conflict_is_refused(lab, tmp_path):
    doc_path = lab / "graphs/lab.json"
    doc = load_doc(doc_path)
    other = {"directed": True, "multigraph": True,
             "graph": {**doc["graph"], "graph_id": "lab:graph/colleague", "title": "A colleague's tests",
                       "stewardship": {"access": "public", "project": PROJECT, "contributed_by": ["lab:org/ni-group"]}},
             "nodes": [*doc["nodes"], {"id": "lab:org/c1", "label": "Institution",
                                        "props": {"name": "c1", "origin": "curated"}}],
             "links": list(doc["links"])}
    write_canonical(tmp_path / "colleague.json", other)
    before = doc_path.read_bytes()
    v = add(tmp_path / "colleague.json", doc_path)
    assert v["status"] == "validated", v["validation"]
    assert v["validation"]["valid"] and doc_path.read_bytes() == before
    m = add(tmp_path / "colleague.json", doc_path, merge=True)
    assert m["status"] == "merged" and m["added_ids"] == ["lab:org/c1"]
    merged = load_doc(doc_path)
    c1 = next(n for n in merged["nodes"] if n["id"] == "lab:org/c1")
    assert c1["props"]["stewardship"]["access"] == "public"         # it keeps the audience it had
    assert valid(doc_path)
    other["nodes"][-1]["props"]["name"] = "c1, renamed"
    write_canonical(tmp_path / "colleague.json", other)
    before = doc_path.read_bytes()
    with pytest.raises(ScaleError, match="other content"):
        add(tmp_path / "colleague.json", doc_path, merge=True)
    assert doc_path.read_bytes() == before
    # an institution's records never enter a project repository through a merge
    other["nodes"][-1] = {"id": "lab:org/c2", "label": "Institution", "props": {"name": "c2", "origin": "curated"}}
    other["graph"]["stewardship"] = {"access": "institution", "project": PROJECT, "contributed_by": ["lab:org/ni-group"]}
    write_canonical(tmp_path / "colleague.json", other)
    with pytest.raises(ScaleError, match="may not read it"):
        add(tmp_path / "colleague.json", doc_path, merge=True)


# ------------------------------------------------------------------------------------------ the tool
def test_the_tool_routes_and_is_gated_by_the_destinations_access(lab, tmp_path):
    doc_path = lab / "graphs/lab.json"
    doc = load_doc(doc_path)
    doc["nodes"][0]["props"]["note"] = CANARY
    write_canonical(doc_path, doc)
    before = doc_path.read_bytes()
    denied = ScaleToolkit(workspace=lab, repo=REPO, egress=EgressGate("anthropic", []))
    out = denied.call("scale_add_source", {"source": str(X / "kinetics.csv"), "into": "graphs/lab.json",
                                           "recorded_at": "2026-03-02T10:00:00Z"})
    assert "egress denied" in out.get("error", "") and CANARY not in json.dumps(out)
    assert doc_path.read_bytes() == before
    ok = ScaleToolkit(workspace=lab, repo=REPO, egress=EgressGate("local", [{"id": "local", "access": ["project"]}]))
    q = ok.call("scale_add_source", {"source": str(X / "co2rr-fe-wide.xlsx"), "into": "graphs/lab.json"})
    assert q["status"] == "questions"
    res = ok.call("scale_add_source", {**q["retry_tool"]["arguments"], "recorded_at": "2026-03-02T10:00:00Z"})
    assert res["status"] == "added", res
    assert ok.call("scale_add_source", {"into": "graphs/lab.json", "bogus": 1})["error"].startswith("unknown arguments")
    # a public graph in the working directory needs no approval
    ws = tmp_path / "ws"
    ws.mkdir()
    pub = ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate("anthropic", []))
    made = pub.call("scale_data_new", {"file": "pub.json", "project": "lab:project/p", "contributor": "lab:org/x",
                                      "access": "public"})
    assert made["valid"], made
    out = pub.call("scale_add_source", {"source": str(X / "dft-energies.csv"), "into": "pub.json", "origin": "computed",
                                        "confirm": CONFIRMED})
    assert out["status"] == "added", out


def test_the_policy_text_and_readme_say_the_writers_apply_the_defaults(lab):
    text = (lab / "README.md").read_text()
    assert "scale add-source" in text and "set it yourself" not in text.lower()
    pol = audience.load_policy(lab / "scale-data.yaml")
    assert pol["defaults"]["stewardship"]["project"] == PROJECT
    assert load_yaml(lab / "scale-data.yaml")["defaults"]["stewardship"]["access"] == "project"


# ------------------------------------------------------------------------------------------ what is refused, and when
def test_a_file_of_a_narrower_repository_never_enters_a_wider_one(tmp_path, lab):
    inst = tmp_path / "inst"
    datarepo.init(inst, access="institution", institutions=["lab:org/ni-group"])
    shutil.copy(X / "kinetics.csv", inst / "raw" / "k.csv")
    (inst / "raw" / "paper.txt").write_text(TEXT)
    before = (lab / "graphs/lab.json").read_bytes()
    for f in ("k.csv", "paper.txt"):
        with pytest.raises(ScaleError, match="fewer readers"):
            add(inst / "raw" / f, lab / "graphs/lab.json", recorded_at="2026-03-02T10:00:00Z")
    assert (lab / "graphs/lab.json").read_bytes() == before and not (lab / "raw/k.csv").exists()
    # the agent tool reads a graph by what it is, whatever its name: a private graph named .txt is gated too
    private = load_doc(inst / "graphs/lab.json")
    private["nodes"][0]["props"]["note"] = CANARY
    write_canonical(inst / "raw" / "graph.txt", private)
    ws = tmp_path / "ws"
    ws.mkdir()
    datarepo.new_document(ws / "pub.json", project="lab:project/p", contributor="lab:org/x", access="public", repo=REPO)
    tk = ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate("anthropic", []))
    out = tk.call("scale_add_source", {"source": str(inst / "raw" / "graph.txt"), "into": "pub.json"})
    assert "egress denied" in out.get("error", "") and CANARY not in json.dumps(out)
    out = ScaleToolkit(workspace=inst / "work", repo=REPO, egress=EgressGate("anthropic", [])).call("scale_template", {})
    assert "egress denied" in out.get("error", "")


def test_source_start_refuses_the_framework_and_a_narrower_project_in_a_repository(lab, tmp_path):
    (tmp_path / "paper.txt").write_text(TEXT)
    with pytest.raises(ScaleError, match="SCALE framework repository"):
        curate.start(REPO / "graphs" / "x-folder", text=tmp_path / "paper.txt", repo=REPO,
                     stewardship={"access": "project", "project": "lab:project/p", "contributed_by": ["lab:org/x"]})
    assert not (REPO / "graphs" / "x-folder").exists()
    with pytest.raises(ScaleError, match="fewer readers|may be read only"):
        curate.start(lab / "work/sources/other", text=tmp_path / "paper.txt", repo=REPO, project="lab:project/other")
    assert not (lab / "work/sources/other").exists()


def test_writes_through_a_link_that_leads_elsewhere_are_refused(lab, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(lab / "work")
    (lab / "work").symlink_to(elsewhere)
    (tmp_path / "paper.txt").write_text(TEXT)
    with pytest.raises(ScaleError, match="outside"):
        add(tmp_path / "paper.txt", lab / "graphs/lab.json")
    assert not any(elsewhere.iterdir())


def test_nothing_is_written_when_a_late_check_fails(lab, tmp_path):
    doc_path = lab / "graphs/lab.json"
    # a different file holds the raw copy's names: refused before anything is written
    (lab / "raw" / "kinetics.csv").write_text("other bytes\n")
    from scale.datatable import sha256
    digest = sha256((X / "kinetics.csv").read_bytes())[:12]
    (lab / "raw" / f"kinetics-{digest}.csv").write_text("corrupt\n")
    before = doc_path.read_bytes()
    with pytest.raises(ScaleError, match="other bytes"):
        add(X / "kinetics.csv", doc_path, recorded_at="2026-03-02T10:00:00Z")
    assert doc_path.read_bytes() == before and not (lab / "work/mappings").exists()
    # a rights entry that differs from the destination's refuses the merge before the graph is written
    g = load_doc(doc_path)
    g["graph"]["graph_id"] = "lab:graph/other"
    g["nodes"].append({"id": "ex:src/1", "label": "Source", "props": {"name": "s", "kind": "article", "title": "S", "origin": "curated"}})
    (tmp_path / "g").mkdir()
    write_canonical(tmp_path / "g" / "other.json", g)
    (tmp_path / "g" / "rights.yaml").write_text("sources:\n  ex:src/1: {class: open, licence: CC-BY-4.0, basis: stated}\n")
    (lab / "rights.yaml").write_text("sources:\n  ex:src/1: {class: in_copyright, licence: none, basis: none found}\n")
    with pytest.raises(ScaleError, match="another rights entry"):
        add(tmp_path / "g" / "other.json", doc_path, merge=True)
    assert doc_path.read_bytes() == before
    (lab / "graphs" / "broken.json").write_text("{not json")
    with pytest.raises(ScaleError, match="not a readable"):
        add(X / "kinetics.csv", lab / "graphs" / "broken.json")


def test_a_second_merge_and_a_second_paper_of_the_same_name(lab, tmp_path):
    doc_path = lab / "graphs/lab.json"
    doc = load_doc(doc_path)
    other = {**doc, "graph": {**doc["graph"], "graph_id": "lab:graph/c", "stewardship": {
        "access": "public", "project": PROJECT, "contributed_by": ["lab:org/ni-group"]}},
        "nodes": [*doc["nodes"], {"id": "lab:org/c3", "label": "Institution", "props": {"name": "c3", "origin": "curated"}}]}
    write_canonical(tmp_path / "c.json", other)
    assert add(tmp_path / "c.json", doc_path, merge=True)["status"] == "merged"
    assert add(tmp_path / "c.json", doc_path, merge=True)["status"] == "unchanged"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "paper.txt").write_text(TEXT)
    (tmp_path / "b" / "paper.txt").write_text(TEXT.replace("46.2", "51.0"))
    one = add(tmp_path / "a" / "paper.txt", doc_path)
    two = add(tmp_path / "b" / "paper.txt", doc_path)
    assert one["folder"] != two["folder"] and not two.get("already")
    assert add(tmp_path / "a" / "paper.txt", doc_path)["already"]


def test_a_retry_keeps_the_route_it_was_given(lab, tmp_path):
    (tmp_path / "runs.txt").write_text("run,T (K),rate (mol g-1 s-1)\nA,500,1.2\n")
    q = add(tmp_path / "runs.txt", lab / "graphs/lab.json", kind="table", template="kinetics-rates")
    assert q["status"] == "questions" and "--kind table" in q["retry"]
