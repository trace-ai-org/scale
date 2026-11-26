"""Tabular data into scale-catal documents (scale-catal 0.8.4): the data link (evidence.dataset_cell, Dataset.table),
its validation, the compiler ``scale data add`` and the check ``scale data verify``, on the synthetic example in
domain/examples/scale-catal/data-ingest-examples/ (a CO2RR Faradaic-efficiency table and a DFT adsorption-energy
table)."""

import copy
import json
import subprocess
import sys

import pytest

from conftest import EXAMPLES, REPO, RUN_SCALE, spec_at, spec_of
from scale import dataingest, datatable, pgraph
from scale.api import ScaleToolkit
from scale.util import ScaleError, load_doc, write_canonical

X = EXAMPLES / "scale-catal/data-ingest-examples"
FE_CSV, FE_MAP = X / "co2rr-fe.csv", X / "co2rr-fe.map.yaml"
DFT_CSV, DFT_MAP = X / "dft-adsorption.csv", X / "dft-adsorption.map.yaml"


@pytest.fixture(scope="module")
def spec():
    return spec_of(load_doc(X / "seed.json"))        # the version the example pins (the head until a later bump)


@pytest.fixture()
def seed():
    return load_doc(X / "seed.json")


def plan(doc, spec, csv=FE_CSV, mapping=FE_MAP, data=None, mapping_text=None):
    return dataingest.plan(data if data is not None else csv.read_bytes(),
                           (mapping_text if mapping_text is not None else mapping.read_text()).encode(), doc, spec,
                           csv.name)


def node(doc, nid):
    return next(n for n in doc["nodes"] if n["id"] == nid)


def research(doc: dict) -> dict:
    """The seed as a laboratory's own research_data document (public access, an institution and a project)."""
    doc = copy.deepcopy(doc)
    for r in doc["nodes"] + doc["links"]:
        r["props"]["origin"] = "curated"
    doc["nodes"] += [{"id": "ex:di/org/lab", "label": "Institution", "props": {"name": "A laboratory", "origin": "curated"}},
                     {"id": "ex:di/project/p", "label": "Project", "props": {"name": "A project", "origin": "curated"}}]
    doc["graph"].update(data_status="research_data", stewardship={"access": "public", "contributed_by": ["ex:di/org/lab"],
                                                                  "project": "ex:di/project/p"})
    return doc


# ------------------------------------------------------------------------------------------ the example
def test_the_example_is_built_reproducibly_valid_and_verified(spec, seed):
    doc = plan(seed, spec)["document"]
    doc = plan(doc, spec, DFT_CSV, DFT_MAP)["document"]
    assert doc == load_doc(X / "data-ingest.json")
    rep = pgraph.validate(doc, spec)
    assert rep.errors == [] and rep.warnings == [] and rep.conformance["profiles"]["catalysis"]["conformant"]
    res = datatable.verify(doc, base=X)
    assert res["status"] == "verified" and [d["status"] for d in res["datasets"]] == ["verified", "verified"]
    assert all(d["matched"] == d["checked"] == d["cells"] for d in res["datasets"])


def test_each_value_cites_its_own_cell_by_key(spec, seed):
    doc = plan(seed, spec)["document"]
    ds = node(doc, "ex:di/dataset/co2rr-fe")["props"]
    assert ds["data_kind"] == "table" and ds["sha256"] == datatable.sha256(FE_CSV.read_bytes()) and ds["version"] == "1"
    t = ds["table"]
    assert t["profile"] == "scale_csv_v1" and t["primary_key"] == ["run_id", "result_id"] and t["rows"] == 9
    fe = next(c for c in t["columns"] if c["name"] == "fe_percent")
    assert fe == {"name": "fe_percent", "datatype": "number", "unit": "%", "variable": "ex:di/variable/faradaic-efficiency"}
    assert set(t["ingest"]) == {"mapping_sha256", "tool", "schema_version", "schema_sha256"}
    r1 = node(doc, "ex:di/test/R1")["props"]
    assert r1["origin"] == "synthetic" and r1["recorded_at"] == "2026-03-02T10:00:00Z"
    ev = {e["id"]: e for e in r1["evidence"]}
    co = next(x for x in r1["result_values"] if x["id"] == "fe_co")
    assert co["value"] == 62.5 and co["unit"] == "%" and co["target"] == "ex:di/species/co"
    assert co["produced_by"] == "ex:di/assay/gc" and co["phase"] == "gas" and co["stream"] == "outlet gas"
    cell = ev[co["evidence_ids"][0]]
    assert cell["kind"] == "dataset_row" and cell["source"] == "ex:di/dataset/co2rr-fe" and cell["quote"] == "62.5"
    assert cell["dataset_cell"] == {"column": "fe_percent", "row_key": [{"column": "run_id", "value": "R1"},
                                                                         {"column": "result_id", "value": "fe_co"}]}
    assert [ev[e]["dataset_cell"]["column"] for e in co["evidence_ids"]] == ["fe_percent", "product_id", "assay_id",
                                                                             "phase", "stream"]
    assert ev[r1["electrode_potential"]["evidence_ids"][0]]["dataset_cell"]["column"] == "potential_V"
    links = {(e["type"], e["target"]) for e in doc["links"] if e["source"] == "ex:di/test/R1"}
    assert ("REPORTED_IN", "ex:di/dataset/co2rr-fe") in links and ("USES_CATALYST", "ex:di/sample/ag") in links


def test_recorded_and_computed_origins_go_into_a_research_document(spec, seed):
    doc = research(seed)
    fe = FE_MAP.read_text().replace("origin: synthetic", "origin: recorded")
    doc = plan(doc, spec, mapping_text=fe)["document"]
    dft = DFT_MAP.read_text().replace("origin: synthetic", "origin: computed")
    doc = plan(doc, spec, DFT_CSV, DFT_MAP, mapping_text=dft)["document"]
    assert node(doc, "ex:di/test/R3")["props"]["origin"] == "recorded"
    assert node(doc, "ex:di/computation/C1")["props"]["origin"] == "computed"
    assert node(doc, "ex:di/dataset/co2rr-fe")["props"]["origin"] == "curated"
    assert pgraph.validate(doc, spec).ok
    # a recorded record carries a supplied recording time, never the time of the import
    with pytest.raises(ScaleError, match="recorded_at_column"):
        plan(research(seed), spec, mapping_text=fe.replace("  recorded_at_column: recorded_at\n", ""))
    # the destination's data status decides: synthetic records only in a synthetic example, and only there
    with pytest.raises(ScaleError, match="synthetic example"):
        plan(seed, spec, mapping_text=fe)
    with pytest.raises(ScaleError, match="only into a synthetic example"):
        plan(research(seed), spec)


def test_the_dft_table_keeps_failed_calculations_and_their_missing_energies(spec, seed):
    doc = plan(seed, spec, DFT_CSV, DFT_MAP)["document"]
    c1, c3 = node(doc, "ex:di/computation/C1")["props"], node(doc, "ex:di/computation/C3")["props"]
    assert c1["experiment_status"] == "completed" and "status_reason" not in c1
    assert c1["level_of_theory"]["text_value"] == "RPBE" and c1["configuration"] == "ex:di/dataset/dft-inputs"
    assert c1["name"] == "Adsorption energies, Cu(111)"
    co = next(x for x in c1["result_values"] if x["id"] == "co_adsorption")
    assert co["value"] == -0.52 and co["unit"] == "eV" and co["target"] == "ex:di/reaction/co-adsorption"
    assert "produced_by" not in co                       # produced_by: self, the computation holding it
    assert c3["experiment_status"] == "failed" and c3["status_reason"] == "SCF not converged after 300 steps"
    for x in c3["result_values"]:
        assert x["status"] == "not_measured" and "value" not in x and "unit" not in x     # never zero


# ------------------------------------------------------------------------------------------ what the compiler refuses
def test_duplicate_keys_are_refused(spec, seed):
    data = FE_CSV.read_bytes() + FE_CSV.read_bytes().splitlines(keepends=True)[1]
    with pytest.raises(ScaleError, match="duplicate key: data rows 0 and 9"):
        plan(seed, spec, data=data)
    # a result id twice in one record (a primary key that does not hold the result id)
    m = FE_MAP.read_text().replace("primary_key: [run_id, result_id]", "primary_key: [run_id, result_id, phase]")
    data = FE_CSV.read_bytes() + b"R1,fe_co,2026-03-02T10:00:00Z,-0.8,ex:di/species/co,ex:di/assay/gc,liquid,x,1\n"
    with pytest.raises(ScaleError, match="duplicate key: ex:di/test/R1 has two results 'fe_co'"):
        plan(seed, spec, data=data, mapping_text=m)


def test_reordered_rows_give_the_same_records(spec, seed):
    lines = FE_CSV.read_bytes().splitlines(keepends=True)
    shuffled = lines[0] + b"".join(reversed(lines[1:]))
    a, b = plan(seed, spec)["document"], plan(seed, spec, data=shuffled)["document"]
    strip = lambda d: [n for n in d["nodes"] if n["label"] != "Dataset"]    # noqa: E731  (the bytes, and so the sha256, differ)
    assert strip(a) == strip(b) and a["links"] == b["links"]
    assert node(a, "ex:di/dataset/co2rr-fe")["props"]["sha256"] != node(b, "ex:di/dataset/co2rr-fe")["props"]["sha256"]


def test_conflicting_run_level_properties_are_refused(spec, seed):
    data = FE_CSV.read_bytes().replace(b"R1,fe_h2,2026-03-02T10:00:00Z,-0.8", b"R1,fe_h2,2026-03-02T10:00:00Z,-0.9")
    with pytest.raises(ScaleError, match="conflicting run-level property electrode_potential for ex:di/test/R1"):
        plan(seed, spec, data=data)


def test_incompatible_units_are_refused_through_the_quantity_contracts(spec, seed):
    with pytest.raises(ScaleError, match="not a dimension of faradaic_efficiency"):
        plan(seed, spec, mapping_text=FE_MAP.read_text().replace('unit: "%"', "unit: eV"))
    with pytest.raises(ScaleError, match="not a dimension of electrode_potential"):
        plan(seed, spec, mapping_text=FE_MAP.read_text().replace("unit: V vs RHE", "unit: K"))


def test_unresolved_references_are_refused(spec, seed):
    data = FE_CSV.read_bytes().replace(b"ex:di/species/formate", b"ex:di/species/acetate")
    with pytest.raises(ScaleError, match=r"unresolved references .*ex:di/species/acetate"):
        plan(seed, spec, data=data)
    with pytest.raises(ScaleError, match="unresolved references .*ex:di/variable/fe .*result.variable"):
        plan(seed, spec, mapping_text=FE_MAP.read_text().replace("ex:di/variable/faradaic-efficiency", "ex:di/variable/fe"))


def test_blanks_and_tokens_map_only_to_missingness_or_bounds_never_zero(spec, seed):
    doc = plan(seed, spec)["document"]
    r2 = {x["id"]: x for x in node(doc, "ex:di/test/R2")["props"]["result_values"]}
    r3 = {x["id"]: x for x in node(doc, "ex:di/test/R3")["props"]["result_values"]}
    assert r2["fe_formate"]["status"] == "below_detection_limit" and r2["fe_formate"]["value"] == 0.5
    assert r2["fe_formate"]["as_reported"] == "<0.5"
    assert r3["fe_formate"]["status"] == "not_measured" and "value" not in r3["fe_formate"]
    # a blank no mapping names is refused, and so is a token that is no number
    no_blank = FE_MAP.read_text().replace('    "":\n      status: not_measured\n', "    n.d.:\n      status: not_measured\n")
    with pytest.raises(ScaleError, match=r"'' is not a number \(a blank cell: map \"\" under missing\)"):
        plan(seed, spec, mapping_text=no_blank)
    with pytest.raises(ScaleError, match="'12,5' is not a number"):
        plan(seed, spec, data=FE_CSV.read_bytes().replace(b",62.5", b',"12,5"'))
    # a token is never mapped to a number
    zero = FE_MAP.read_text().replace("      status: not_measured\n", "      status: present\n      value: 0\n")
    with pytest.raises(ScaleError, match="unknown keys"):
        plan(seed, spec, mapping_text=zero)
    zero = FE_MAP.read_text().replace("      status: not_measured\n", "      status: present\n")
    with pytest.raises(ScaleError, match="never becomes a number"):
        plan(seed, spec, mapping_text=zero)


def test_the_mapping_is_declarative_and_versioned(spec, seed):
    with pytest.raises(ScaleError, match=r"the mapping has unknown keys \['script'\]"):
        plan(seed, spec, mapping_text=FE_MAP.read_text() + "script: import os\n")
    with pytest.raises(ScaleError, match="mapping_version 3 is not one this tool reads"):
        plan(seed, spec, mapping_text=FE_MAP.read_text().replace("mapping_version: 1", "mapping_version: 3"))
    with pytest.raises(ScaleError, match="result has unknown keys"):
        plan(seed, spec, mapping_text=FE_MAP.read_text().replace("  phase_column: phase\n", "  phase_column: phase\n  sql: x\n"))
    with pytest.raises(ScaleError, match="names its version or its uri"):
        plan(seed, spec, mapping_text=FE_MAP.read_text().replace('  version: "1"\n', "").replace("  uri: co2rr-fe.csv\n", ""))


def test_the_file_is_read_under_its_profile(spec, seed):
    with pytest.raises(ScaleError, match="names 'phase' more than once"):
        plan(seed, spec, data=FE_CSV.read_bytes().replace(b",stream,", b",phase,", 1))
    with pytest.raises(ScaleError, match="byte-order mark"):
        plan(seed, spec, data=b"\xef\xbb\xbf" + FE_CSV.read_bytes())
    with pytest.raises(ScaleError, match="data row 1 has 8 fields"):
        plan(seed, spec, data=FE_CSV.read_bytes().replace(b",outlet gas,30.1", b",30.1"))


def test_a_row_index_is_an_explicit_opt_in(spec, seed):
    m = FE_MAP.read_text().replace("  primary_key: [run_id, result_id]\n", "  row_selector: index\n")
    doc = plan(seed, spec, mapping_text=m)["document"]
    e = next(e for e in node(doc, "ex:di/test/R1")["props"]["evidence"] if e["dataset_cell"]["column"] == "fe_percent")
    assert e["dataset_cell"] == {"column": "fe_percent", "row_index": 0} and e["locator"] == "row index 0; column fe_percent"
    assert node(doc, "ex:di/dataset/co2rr-fe")["props"]["table"]["primary_key"] == []
    assert pgraph.validate(doc, spec).ok and datatable.verify(doc, base=X)["status"] == "verified"


# ------------------------------------------------------------------------------------------ writing, retries, the CLI
def test_a_retry_is_idempotent_and_a_conflict_is_never_overwritten(spec, seed, tmp_path):
    into = tmp_path / "lab.json"
    write_canonical(into, seed)
    first = dataingest.add(FE_CSV, FE_MAP, into, spec=spec)
    assert first["written"] == str(into) and first["records"] == 3 and first["results"] == 9
    after = into.read_bytes()
    second = dataingest.add(FE_CSV, FE_MAP, into, spec=spec)
    assert second["added"] == [] and second["written"] is None and len(second["unchanged"]) == len(first["added"])
    assert into.read_bytes() == after
    doc = load_doc(into)
    next(x for x in node(doc, "ex:di/test/R1")["props"]["result_values"] if x["id"] == "fe_co")["value"] = 61
    write_canonical(into, doc)
    with pytest.raises(ScaleError, match="already in the document with other content .*ex:di/test/R1"):
        dataingest.add(FE_CSV, FE_MAP, into, spec=spec)
    # new bytes under the id of a Dataset already imported: a new Dataset, never a new checksum on the old one
    changed = tmp_path / "co2rr-fe.csv"
    changed.write_bytes(FE_CSV.read_bytes().replace(b"62.5", b"62.4"))
    with pytest.raises(ScaleError, match="new bytes are a new Dataset"):
        dataingest.add(changed, FE_MAP, into, spec=spec)


def test_a_dry_run_writes_nothing_and_the_cli_prints_what_it_would_add(seed, tmp_path):
    into = tmp_path / "lab.json"
    write_canonical(into, seed)
    before = into.read_bytes()
    run = lambda *a: subprocess.run([sys.executable, "-c", RUN_SCALE, str(REPO / "tools"), *a],  # noqa: E731
                                    capture_output=True, text=True, cwd=REPO)
    p = run("data", "add", str(FE_CSV), "--map", str(FE_MAP), "--into", str(into), "--dry-run", "--format", "json")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["dry_run"] and out["written"] is None and len(out["nodes"]) == 4 and into.read_bytes() == before
    p = run("data", "add", str(FE_CSV), "--map", str(FE_MAP), "--into", str(into))
    assert p.returncode == 0 and "added 16 records and links" in p.stdout and "; written" in p.stdout, p.stdout + p.stderr
    p = run("data", "verify", str(into), "--file", f"ex:di/dataset/co2rr-fe={FE_CSV}")
    assert p.returncode == 0 and "VERIFIED" in p.stdout, p.stdout
    p = run("data", "verify", str(into), "--require-all")             # the uri is relative to tmp_path: no copy there
    assert p.returncode == 1 and "UNVERIFIED" in p.stdout, p.stdout


# ------------------------------------------------------------------------------------------ verification
def test_verification_checks_the_checksum_and_each_cell_and_never_claims_what_it_cannot_see(spec, seed, tmp_path):
    doc = plan(seed, spec)["document"]
    assert datatable.verify(doc, base=tmp_path)["status"] == "unverified"           # no file at hand: never verified
    assert datatable.verify(doc, {"ex:di/dataset/co2rr-fe": FE_CSV})["status"] == "verified"
    tampered = tmp_path / "co2rr-fe.csv"
    tampered.write_bytes(FE_CSV.read_bytes().replace(b"62.5", b"65.2"))
    res = datatable.verify(doc, {"ex:di/dataset/co2rr-fe": tampered})
    assert res["status"] == "failed" and "checksum mismatch" in res["datasets"][0]["problems"][0]
    # a record changed after the import: the bytes match, the cell does not
    changed = copy.deepcopy(doc)
    next(x for x in node(changed, "ex:di/test/R1")["props"]["result_values"] if x["id"] == "fe_co")["value"] = 65.2
    next(x for x in node(changed, "ex:di/test/R2")["props"]["result_values"] if x["id"] == "fe_co")["target"] = "ex:di/species/h2"
    res = datatable.verify(changed, base=X)
    probs = res["datasets"][0]["problems"]
    assert res["status"] == "failed" and len(probs) == 2
    assert "cell '62.5', value 65.2" in probs[0] and "cell 'ex:di/species/co' is none of its" in probs[1]
    with pytest.raises(ScaleError, match="no Dataset of the document"):
        datatable.verify(doc, {"ex:di/dataset/other": FE_CSV})


# ------------------------------------------------------------------------------------------ the schema's rules
def _cell(doc, rid="ex:di/test/R1", eid="fe_percent.R1,fe_co"):
    return next(e for e in node(doc, rid)["props"]["evidence"] if e["id"] == eid)


@pytest.mark.parametrize("change, message", [
    (lambda d: _cell(d).__setitem__("kind", "table"), "goes on evidence of kind dataset_row, not 'table'"),
    (lambda d: node(d, "ex:di/dataset/co2rr-fe")["props"].__setitem__("sha256", "abc"), "is not 64 hexadecimal"),
    (lambda d: [node(d, "ex:di/dataset/co2rr-fe")["props"].pop(k) for k in ("version", "uri")],
     "states neither its version nor its uri"),
    (lambda d: node(d, "ex:di/dataset/co2rr-fe")["props"].pop("table"), "which describes no table"),
    (lambda d: _cell(d)["dataset_cell"].__setitem__("row_index", 0), "exactly one of row_key and row_index"),
    (lambda d: _cell(d)["dataset_cell"].__setitem__("column", "fe"), "column 'fe' is not a column of"),
    (lambda d: _cell(d)["dataset_cell"]["row_key"].append({"column": "phase", "value": "gas"}),
     "not every primary-key column of ex:di/dataset/co2rr-fe once"),
    (lambda d: node(d, "ex:di/dataset/co2rr-fe")["props"]["table"]["columns"][8].pop("variable"),
     "is a numeric column that names no Variable"),
    (lambda d: node(d, "ex:di/variable/faradaic-efficiency")["props"].update(scale="ordinal"),
     "which is not a quantitative Variable with a quantity_kind"),
    (lambda d: node(d, "ex:di/dataset/co2rr-fe")["props"]["table"]["columns"][8].update(unit="K"),
     "not a dimension of faradaic_efficiency"),
    (lambda d: node(d, "ex:di/dataset/co2rr-fe")["props"]["table"]["primary_key"].append("run"),
     "primary_key column 'run' is not a column of the table"),
    (lambda d: node(d, "ex:di/dataset/co2rr-fe")["props"]["table"]["columns"].append({"name": "phase", "datatype": "string"}),
     "column 'phase' is named more than once"),
    (lambda d: _cell(d)["dataset_cell"].pop("row_key") and _cell(d)["dataset_cell"].__setitem__("row_index", 9),
     "row_index 9 is not a data row"),
])
def test_the_data_link_rules_are_errors(spec, change, message):
    doc = copy.deepcopy(load_doc(X / "data-ingest.json"))
    change(doc)
    errors = [e["message"] for e in pgraph.validate(doc, spec).errors]
    assert any(message in m for m in errors), errors


def test_legacy_documents_stay_valid_and_the_old_schema_knows_no_cell(spec):
    # a dataset_row evidence item without a cell is valid as before; the cell is optional
    doc = copy.deepcopy(load_doc(X / "data-ingest.json"))
    for n in doc["nodes"]:
        for e in n["props"].get("evidence") or []:
            e.pop("dataset_cell", None)
    assert pgraph.validate(doc, spec).ok
    # every committed example stays valid under the version it pins
    for p in sorted((EXAMPLES / "scale-catal").rglob("*.json")):
        d = load_doc(p)
        if "nodes" in d and d.get("graph", {}).get("schema") == "scale-catal":
            assert pgraph.validate(d, pgraph.load_spec(pgraph.spec_for(REPO, d["graph"]["schema_version"], doc=d))).ok, p
    # the version before has no cell: a document using one is not of that version
    prev = load_doc(X / "data-ingest.json")
    old = spec_at("0.8.3")
    assert "dataset_cell" not in old and "dataset_cell" not in old["evidence"]["fields"]
    with pytest.raises(ScaleError, match="has no dataset cells"):
        dataingest.plan(FE_CSV.read_bytes(), FE_MAP.read_bytes(), {**prev, "graph": {**prev["graph"], "schema_version": "0.8.3"}},
                        old)


def test_a_public_view_withholds_a_dataset_less_open_and_the_values_it_alone_states(spec):
    doc = copy.deepcopy(load_doc(X / "data-ingest.json"))
    doc["nodes"].append({"id": "ex:di/project/lab", "label": "Project", "props": {"name": "Lab", "origin": "synthetic"}})
    node(doc, "ex:di/dataset/dft-adsorption")["props"]["stewardship"] = {"access": "project", "project": "ex:di/project/lab"}
    view = pgraph.filter_for(doc, [], "2026-11-25")
    ids = {n["id"] for n in view["nodes"]}
    assert "ex:di/dataset/dft-adsorption" not in ids and "ex:di/dataset/co2rr-fe" in ids
    c1 = node(view, "ex:di/computation/C1")["props"]
    assert "evidence" not in c1 and all(x == {"status": "not_reported", "missing_reason": "withheld", **{
        k: x[k] for k in ("id", "target", "variable") if k in x}} for x in c1["result_values"])
    assert not any(e["target"] == "ex:di/dataset/dft-adsorption" for e in view["links"])
    assert pgraph.validate(view, spec).ok
    assert datatable.verify(view, base=X)["status"] == "verified"        # what the view keeps still verifies


def test_a_query_returns_the_cells_a_result_cites(spec):
    from scale import pgraph_query
    plan_ = {"question": "Which CO Faradaic efficiencies were recorded?", "match": {"label": "CatalyticTest", "as": "test"},
             "steps": [{"from": "test", "entries": "result_values", "kind": "faradaic_efficiency", "as": "result"},
                       {"from": "result", "field": "target", "to": "ChemicalSpecies", "as": "product",
                        "where": [{"prop": "formula", "op": "eq", "value": "CO"}]}],
             "return": ["test.name", "result"], "evidence": True}
    res = pgraph_query.run(load_doc(X / "data-ingest.json"), spec, plan_)
    assert len(res["rows"]) == 3
    row = next(r for r in res["rows"] if r["bound"]["test"] == "ex:di/test/R1")
    cell = next(e for e in row["evidence"] if e["dataset_cell"]["column"] == "fe_percent")
    assert cell["source"] == "ex:di/dataset/co2rr-fe" and cell["quote"] == "62.5" and cell["source_kind"] == "dataset"
    assert cell["dataset_cell"]["row_key"] == [{"column": "run_id", "value": "R1"}, {"column": "result_id", "value": "fe_co"}]


def test_the_toolkit_adds_and_verifies_inside_its_working_directory(seed, tmp_path):
    tk = ScaleToolkit(workspace=tmp_path / "ws", repo=REPO)
    (tmp_path / "ws").mkdir()
    write_canonical(tmp_path / "ws/lab.json", seed)
    dry = tk.call("scale_data_add", {"csv": str(FE_CSV), "mapping": str(FE_MAP), "into": "lab.json", "dry_run": True})
    assert dry["written"] is None and len(dry["nodes"]) == 4
    res = tk.call("scale_data_add", {"csv": str(FE_CSV), "mapping": str(FE_MAP), "into": "lab"})
    assert res["written"] == str(tmp_path / "ws/lab.json")
    assert tk.call("scale_data_add", {"csv": str(FE_CSV), "mapping": str(FE_MAP), "into": "../x"})["error"]
    got = tk.call("scale_data_verify", {"path": "lab.json", "files": {"ex:di/dataset/co2rr-fe": str(FE_CSV)}})
    assert got["status"] == "verified"
    assert tk.call("scale_data_verify", {"path": "lab.json"})["status"] == "unverified"


# ------------------------------------------------------------------------------------------ edge cases (review)
def test_keys_that_join_alike_never_share_an_evidence_id(spec, seed, tmp_path):
    m = FE_MAP.read_text().replace("primary_key: [run_id, result_id]", "primary_key: [run_id, stream, result_id]")
    lines = FE_CSV.read_text().splitlines(keepends=True)
    data = (lines[0] + lines[1].replace(",outlet gas,", ",a-b,").replace(",fe_co,", ",c,")
            + lines[2].replace(",outlet gas,", ",a,").replace(",fe_h2,", ",b_c,")).encode()
    doc = plan(seed, spec, data=data, mapping_text=m)["document"]
    ev = node(doc, "ex:di/test/R1")["props"]["evidence"]
    assert len({e["id"] for e in ev}) == len(ev)
    f = tmp_path / "t.csv"
    f.write_bytes(data)
    assert datatable.verify(doc, {"ex:di/dataset/co2rr-fe": f})["status"] == "verified"
    assert dataingest._seg("a,b") == "a%2Cb" and dataingest._seg("a/b") == "a%2Fb"


def test_a_declared_key_holds_and_a_declared_datatype_is_checked(spec, seed):
    m = FE_MAP.read_text().replace("  primary_key: [run_id, result_id]\n", "  row_selector: index\n  primary_key: [run_id]\n")
    with pytest.raises(ScaleError, match="duplicate key"):
        plan(seed, spec, mapping_text=m)
    m = FE_MAP.read_text().replace("  licence: CC-BY-4.0\n", "  licence: CC-BY-4.0\n  columns:\n    fe_percent:\n"
                                                             "      datatype: integer\n")
    with pytest.raises(ScaleError, match="column 'fe_percent' is declared integer, and its cell '62.5' is not one"):
        plan(seed, spec, mapping_text=m)


def test_a_result_id_or_a_number_with_a_trailing_newline_is_refused(spec, seed):
    with pytest.raises(ScaleError, match="is not snake_case"):
        plan(seed, spec, data=FE_CSV.read_bytes().replace(b"R1,fe_co,", b'R1,"fe_co\n",', 1))
    with pytest.raises(ScaleError, match="is not a number"):
        plan(seed, spec, data=FE_CSV.read_bytes().replace(b",62.5", b',"62.5\n"', 1))


def test_verification_reads_tokens_ranges_integers_and_text_blanks_strictly(spec, seed):
    doc = plan(seed, spec)["document"]
    censored = copy.deepcopy(doc)
    x = next(x for x in node(censored, "ex:di/test/R2")["props"]["result_values"] if x["id"] == "fe_formate")
    x["status"] = "present"                              # "<0.5" read as an exact 0.5
    res = datatable.verify(censored, base=X)
    assert res["status"] == "failed" and "is a token" in res["datasets"][0]["problems"][0]
    ranged = copy.deepcopy(doc)
    x = next(x for x in node(ranged, "ex:di/test/R1")["props"]["result_values"] if x["id"] == "fe_co")
    del x["value"]
    x.update(lower=62, upper=63)
    res = datatable.verify(ranged, base=X)
    assert res["status"] == "unverified" and res["datasets"][0]["matched"] == res["datasets"][0]["checked"] - 1
    assert datatable._holds("9007199254740992", {"status": "present", "value": 9007199254740993}, True)[0] is False
    assert datatable._holds("62.50", {"status": "present", "value": 62.5}, True)[0] is True
    assert datatable._holds("", {"status": "not_measured"}, False)[0] is True


def _cell2(doc, eid):
    return next(e for e in node(doc, "ex:di/test/R1")["props"]["evidence"] if e["id"] == eid)


def test_malformed_tables_and_cells_are_reported_not_raised(spec):
    doc = copy.deepcopy(load_doc(X / "data-ingest.json"))
    node(doc, "ex:di/dataset/co2rr-fe")["props"]["table"]["columns"] = 1
    _cell2(doc, "fe_percent.R1,fe_co")["dataset_cell"]["column"] = []
    assert not pgraph.validate(doc, spec).ok
    datatable.verify(doc, base=X)


def test_a_view_keeps_table_text_that_equals_a_withheld_id(spec):
    doc = copy.deepcopy(load_doc(X / "data-ingest.json"))
    doc["nodes"].append({"id": "ex:di/project/lab", "label": "Project", "props": {"name": "Lab", "origin": "synthetic"}})
    doc["nodes"].append({"id": "x:run", "label": "Dataset", "props": {
        "name": "private", "data_kind": "raw", "availability": "restricted", "origin": "synthetic",
        "stewardship": {"access": "project", "project": "ex:di/project/lab"}}})
    t = node(doc, "ex:di/dataset/dft-adsorption")["props"]["table"]
    t["columns"][0]["name"] = "x:run"
    t["primary_key"][0] = "x:run"
    view = pgraph.filter_for(doc, [], "2026-11-25")
    vt = node(view, "ex:di/dataset/dft-adsorption")["props"]["table"]
    assert vt["columns"][0]["name"] == "x:run" and vt["primary_key"][0] == "x:run"
