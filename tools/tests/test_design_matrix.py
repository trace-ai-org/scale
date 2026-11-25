"""The design bridge: a typed matrix for experiment-design agents (tools/designmatrix.py) and the write-back of their
proposals and results (tools/campaign.py); docs/querying.md, "The design matrix". Synthetic documents only."""

import copy
import csv
import functools
import io
import json

import pytest

from conftest import HEAD_VERSION, REPO
from scale import campaign, designmatrix, federation, pgraph
from scale.cli import main
from scale.util import ScaleError, load_doc

PM = REPO / "domain/examples/scale-catal/product-measurement-examples/product-measurements.json"
RECORDED = federation.recorded_digests(REPO)
CO_MAP = {"identity_map": 1, "mappings": [
    {"id": "species-co", "records": [{"graph": "public", "record": "ex:pm/species/co"},
                                     {"graph": "private", "record": "lab:pm/species/co"}],
     "evidence": [{"basis": "the same formula (CO) and charge (0) in both graphs"}]}]}

CO_PLAN = {"question": "Which CO Faradaic efficiencies were measured, at which potential?",
           "match": {"label": "CatalyticTest", "as": "test"},
           "steps": [{"from": "test", "entries": "result_values", "kind": "faradaic_efficiency", "as": "fe"},
                     {"from": "fe", "field": "target", "to": "ChemicalSpecies", "as": "product",
                      "where": [{"prop": "formula", "op": "eq", "value": "CO"}]},
                     {"from": "fe", "field": "produced_by", "to": "Experiment", "as": "assay", "optional": True},
                     {"from": "test", "edge": "USES_CATALYST", "to": "CatalystSample", "as": "sample"}],
           "return": ["test.name"]}
CONTRACT = {"contract": 1, "name": "co-fe", "row": {"var": "fe", "interval": "assay.name"},
            "runs": {"include": ["completed", "not_reported"]},
            "features": [{"name": "potential", "path": "test.electrode_potential", "type": "quantity", "unit": "V",
                          "bounds": {"lower": -1.5, "upper": 0}},
                         {"name": "control", "path": "test.control_mode", "type": "category"}],
            "outcomes": [{"name": "fe_co", "path": "fe", "type": "quantity", "unit": "%", "objective": "maximize",
                          "variable": "ex:pm/variable/faradaic-efficiency"}],
            "groups": [{"name": "specimen", "path": "sample"}]}


@functools.lru_cache(maxsize=None)
def _spec(version):
    return pgraph.load_spec(pgraph.spec_for(REPO, version))


def members(**docs):
    return [federation.member(a, doc=d, spec=_spec(d["graph"]["schema_version"]), recorded=RECORDED) for a, d in docs.items()]


@pytest.fixture(scope="module")
def public():
    return load_doc(PM)


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_a_matrix_has_explicit_rows_typed_cells_provenance_and_its_access(public):
    text, man = designmatrix.build(members(public=public), CO_PLAN, CONTRACT)
    rows = rows_of(text)
    assert [r["row_id"] for r in rows] == sorted(r["row_id"] for r in rows)
    assert all(r["row_id"] == f"public::{r['record']}#{r['entry']}" and r["entry"].startswith("/result_values/") for r in rows)
    assert {r["fe_co__status"] for r in rows} == {"present"} and all(float(r["fe_co"]) >= 0 for r in rows)
    assert all(r["potential__status"] == "present" and r["group:specimen"].startswith("public::") for r in rows)
    gc = [r for r in rows if r["record"] == "ex:pm/test/co2rr"]
    assert len(gc) == 2 and len({r["interval"] for r in gc}) == 2      # the GC at 30 and at 60 min: two rows, never averaged
    cols = {c["name"]: c for c in man["columns"]}
    assert cols["fe_co"]["role"] == "outcome" and cols["fe_co"]["objective"] == "maximize" and cols["fe_co"]["unit"] == "%"
    assert cols["potential"]["role"] == "feature" and cols["potential"]["bounds"] == {"lower": -1.5, "upper": 0}
    assert man["graphs"][0]["spec_verified"] is True and man["graphs"][0]["graph_sha256"]
    assert man["files"]["matrix.csv"]["sha256"] == designmatrix._sha(text.encode()) and man["access"]["level"] == "public"
    assert man["split"]["group_by"] == ["specimen"] and man["transformations"] == []
    assert all(r["evidence"]["fe_co"] for r in man["rows"])            # each outcome cell names what states it
    # the repeated electrolysis's two CO results that nothing tells apart are two rows, flagged, never averaged
    rep = [r for r in man["rows"] if r["record"] == "ex:pm/test/co2rr-repeat"]
    assert len(rep) == 2 and all(len(r["ambiguous_repetition"]) == 1 for r in rep)
    assert man["excluded_by_query"]["public"]["in_steps:unresolved_target"] == 1        # counted, by reason


def test_typed_missingness_failed_runs_and_censoring(public):
    doc = copy.deepcopy(public)
    t = next(n for n in doc["nodes"] if n["id"] == "ex:pm/test/co2rr")
    t["props"]["electrode_potential"] = {"status": "not_reported", "missing_reason": "not_stated_in_source"}
    co = [v for v in t["props"]["result_values"] if v.get("target") == "ex:pm/species/co"]
    co[0].update(bound="lower", value=40)
    co[1].update(uncertainty=2, uncertainty_kind="standard_deviation")
    text, man = designmatrix.build(members(public=doc), CO_PLAN, CONTRACT)
    rows = {r["entry"]: r for r in rows_of(text) if r["record"] == "ex:pm/test/co2rr"}
    assert {r["potential__status"] for r in rows.values()} == {"not_reported:not_stated_in_source"}
    assert all(r["potential"] == "" for r in rows.values())            # never imputed
    cens = rows[f"/result_values/{co[0]['id']}"]
    assert cens["fe_co__status"] == "censored_lower" and cens["fe_co__lower"] == "40" and cens["fe_co"] == ""
    unc = rows[f"/result_values/{co[1]['id']}"]
    assert unc["fe_co__uncertainty"] == "2" and unc["fe_co__uncertainty_kind"] == "standard_deviation"
    assert man["missingness"]["potential"] == {"not_reported:not_stated_in_source": 2, "present": 2}


def test_ambiguous_pivots_and_incompatible_outcomes_are_refused(public):
    test_rows = dict(CONTRACT, row={"var": "test"})
    with pytest.raises(ScaleError, match="ambiguous pivot"):            # two CO results of one test, one column
        designmatrix.build(members(public=public), CO_PLAN, test_rows)
    every = copy.deepcopy(CO_PLAN)
    del every["steps"][1]["where"]                                     # every product's efficiency in one column
    with pytest.raises(ScaleError, match="comparison contexts"):
        designmatrix.build(members(public=public), every, CONTRACT)
    other_unit = copy.deepcopy(CONTRACT)
    other_unit["outcomes"][0]["unit"] = "mol%"
    with pytest.raises(ScaleError, match="never converted"):
        designmatrix.build(members(public=public), CO_PLAN, other_unit)
    unchecked = copy.deepcopy(CONTRACT)
    unchecked["features"][0]["context"] = {"potential_reference": "she"}
    with pytest.raises(ScaleError, match="not on the contract's"):    # potentials on RHE are not on SHE, never converted
        designmatrix.build(members(public=public), CO_PLAN, unchecked)
    for bad in ({**CONTRACT, "outcomes": []}, {**CONTRACT, "contract": 2}, {**CONTRACT, "runs": {"include": ["proposed"]}},
                {**CONTRACT, "features": [{"name": "t", "path": "test.temperature", "type": "quantity"}]},
                {**CONTRACT, "features": [{"name": "fe_co", "path": "test.name", "type": "text"}]}):
        with pytest.raises(ScaleError):
            designmatrix.load_contract(bad)


def test_the_matrix_is_deterministic_and_written_canonically(public, tmp_path):
    priv = json.loads(json.dumps(public).replace("ex:pm/", "lab:pm/"))
    priv["graph"]["stewardship"] = {"access": "project", "project": "lab:project/p1"}
    con = copy.deepcopy(CONTRACT)
    con["outcomes"][0].pop("variable")
    with pytest.raises(ScaleError, match="comparison contexts"):     # CO of two graphs is one species only by a map
        designmatrix.build(members(public=public, private=priv), CO_PLAN, con)
    imap = federation.load_identity_map(CO_MAP)
    a = designmatrix.build(members(public=public, private=priv), CO_PLAN, con, imap)
    b = designmatrix.build(members(public=public, private=priv), CO_PLAN, con, imap)
    assert a[1]["identity_map_sha256"] == imap["sha256"]
    assert a[0] == b[0] and json.dumps(a[1], sort_keys=True) == json.dumps(b[1], sort_keys=True)
    assert {r["graph"] for r in rows_of(a[0])} == {"public", "private"}
    assert a[1]["access"]["level"] == "project"                       # the matrix goes where every graph may go
    for d in (public, priv):
        (tmp_path / f"{'pub' if d is public else 'priv'}.json").write_text(json.dumps(d))
    (tmp_path / "plan.json").write_text(json.dumps(CO_PLAN))
    (tmp_path / "contract.json").write_text(json.dumps(con))
    (tmp_path / "map.json").write_text(json.dumps(CO_MAP))
    args = ["--repo", str(REPO), "data", "matrix", "--graph", f"public={tmp_path / 'pub.json'}", "--graph",
            f"private={tmp_path / 'priv.json'}", "--plan", str(tmp_path / "plan.json"), "--contract",
            str(tmp_path / "contract.json"), "--identity-map", str(tmp_path / "map.json")]
    assert main(args + ["--out", str(tmp_path / "m1")]) == 0 and main(args + ["--out", str(tmp_path / "m2")]) == 0
    for f in ("matrix.csv", "matrix.manifest.json"):
        assert (tmp_path / "m1" / f).read_bytes() == (tmp_path / "m2" / f).read_bytes()
    assert (tmp_path / "m1/matrix.csv").read_text() == a[0]
    assert json.loads((tmp_path / "m1/matrix.manifest.json").read_text())["files"] == a[1]["files"]


# ---------------------------------------------------------------- write-back

def synthetic_campaign() -> dict:
    """A campaign's own graph before any proposal: its design, its agent, the variable it optimizes, the product and
    the run log the results will cite. Invented, built here and never committed: a campaign's records are its own
    (recorded, hypothesized), so it is research data in form, public in this test."""
    return {"directed": True, "multigraph": True,
            "graph": {"schema": "scale-catal", "schema_version": HEAD_VERSION, "graph_id": "ex:camp/graph",
                      "title": "An invented campaign (synthetic)", "data_status": "research_data",
                      "follows_domains": ["catalysis"],
                      "stewardship": {"access": "public", "contributed_by": ["ex:camp/org/lab"],
                                      "project": "ex:camp/project/p"}},
            "nodes": [
                {"id": "ex:camp/org/lab", "label": "Institution", "props": {"name": "An invented lab", "origin": "curated"}},
                {"id": "ex:camp/project/p", "label": "Project", "props": {"name": "An invented project", "origin": "curated"}},
                {"id": "ex:camp/design", "label": "Design", "props": {
                    "name": "CO2 electroreduction campaign", "design_kind": "adaptive", "assignment_method": "adaptive",
                    "autonomy_level": "A2", "objectives": "maximize the CO Faradaic efficiency", "origin": "curated"}},
                {"id": "ex:camp/agent", "label": "Software", "props": {"name": "BO agent 1.0", "origin": "curated"}},
                {"id": "ex:camp/variable/fe", "label": "Variable", "props": {
                    "name": "Faradaic efficiency", "scale": "quantitative", "quantity_kind": "faradaic_efficiency",
                    "unit": "%", "origin": "curated"}},
                {"id": "ex:camp/species/co", "label": "ChemicalSpecies", "props": {
                    "name": "CO", "formula": "CO", "charge": 0, "elemental_composition": {"C": 1, "O": 1},
                    "origin": "curated"}},
                {"id": "ex:camp/dataset/runs", "label": "Dataset", "props": {
                    "name": "Run log", "data_kind": "table", "availability": "not_reported", "origin": "curated"}}],
            "links": []}


def proposal(i, potential):
    return {"id": f"ex:camp/run/{i}", "label": "CatalyticTest", "name": f"Run {i} at {potential} V vs RHE",
            "props": {"test_kind": "activity", "driving_forces": ["electro"], "control_mode": "potentiostatic", "reference_scale": "rhe",
                      "electrode_potential": {"status": "present", "value": potential, "unit": "V"}},
            "rationale": "highest expected improvement", "acquisition_value": 0.4 - i / 10,
            "prediction": {"id": f"ex:camp/prediction/{i}", "statement": "CO above 70 %", "variable": "ex:camp/variable/fe",
                           "expected": {"status": "present", "value": 75, "unit": "%"}, "direction": "value"}}


MATRIX_PLAN = {"match": {"label": "CatalyticTest", "as": "test"},
               "steps": [{"from": "test", "entries": "result_values", "kind": "faradaic_efficiency", "as": "fe",
                          "optional": True},
                         {"from": "test", "edge": "PART_OF_DESIGN", "to": "Design", "as": "design", "edge_as": "round"}],
               "return": ["test.name"]}
MATRIX_CONTRACT = {"contract": 1, "name": "campaign", "row": {"var": "test", "replicate": "round.iteration"},
                   "runs": {"include": ["completed", "failed"]},
                   "features": [{"name": "potential", "path": "test.electrode_potential", "type": "quantity", "unit": "V"}],
                   "outcomes": [{"name": "fe_co", "path": "fe", "type": "quantity", "unit": "%", "objective": "maximize"}],
                   "groups": [{"name": "campaign", "path": "design"}]}


def test_proposals_and_results_round_trip_as_an_auditable_campaign():
    spec = _spec(HEAD_VERSION)
    doc = campaign.propose(synthetic_campaign(), design="ex:camp/design", iteration=1, agent="ex:camp/agent",
                           proposed_at="2026-11-02T09:00:00+00:00", proposals=[proposal(1, -0.8), proposal(2, -1.0)])
    rep = pgraph.validate(doc, spec).as_dict()
    assert rep["valid"], rep["errors"]
    types = sorted(e["type"] for e in doc["links"])
    assert types == sorted(["PROPOSED_BY"] * 4 + ["PART_OF_DESIGN"] * 2 + ["AIMS_TO_TEST"] * 2)
    pred = next(n for n in doc["nodes"] if n["id"] == "ex:camp/prediction/1")["props"]
    assert pred["proposed_at"] == "2026-11-02" and pred["expected"]["origin"] == "predicted"
    # proposals are never observations: the matrix has no row, and says why each was left out
    text, man = designmatrix.build(members(camp=doc), MATRIX_PLAN, MATRIX_CONTRACT)
    assert rows_of(text) == [] and {x["reason"] for x in man["excluded"]} == {"proposal_not_observation"}

    ev = {"id": "run1_fe", "source": "ex:camp/dataset/runs", "kind": "dataset_row", "locator": "run 1, column fe_co"}
    doc = campaign.record_result(doc, proposal="ex:camp/run/1", result_id="ex:camp/run/1/result",
                                 recorded_at="2026-11-03T15:00:00+00:00", outcome="supports",
                                 result_refs=[{"record": "ex:camp/run/1/result", "path": "/result_values/co_fe"}],
                                 props={"result_values": [{"id": "co_fe", "status": "present", "value": 78, "unit": "%",
                                                           "variable": "ex:camp/variable/fe", "target": "ex:camp/species/co",
                                                           "evidence_ids": ["run1_fe"]}]},
                                 evidence=[ev])
    doc = campaign.record_result(doc, proposal="ex:camp/run/2", result_id="ex:camp/run/2/result",
                                 recorded_at="2026-11-03T16:00:00+00:00", status="failed",
                                 status_reason="the electrolyte leaked after 5 min")
    rep = pgraph.validate(doc, spec).as_dict()
    assert rep["valid"], rep["errors"]
    links = {(e["type"], e["source"], e["target"]): e["props"] for e in doc["links"]}
    assert ("REVISES", "ex:camp/run/1/result", "ex:camp/run/1") in links
    assert links[("TESTS", "ex:camp/run/1/result", "ex:camp/prediction/1")]["outcome"] == "supports"
    assert links[("TESTS", "ex:camp/run/2/result", "ex:camp/prediction/2")]["outcome"] == "inconclusive"
    assert links[("PART_OF_DESIGN", "ex:camp/run/2/result", "ex:camp/design")]["iteration"] == 1
    kept = next(n for n in doc["nodes"] if n["id"] == "ex:camp/run/1")["props"]
    assert kept["experiment_status"] == "proposed" and kept["origin"] == "hypothesized"     # the proposal as it was
    result = next(n for n in doc["nodes"] if n["id"] == "ex:camp/run/1/result")["props"]
    assert result["origin"] == "recorded" and result["record_version"] == 2 and result["electrode_potential"]["value"] == -0.8

    text, man = designmatrix.build(members(camp=doc), MATRIX_PLAN, MATRIX_CONTRACT)
    rows = {r["record"]: r for r in rows_of(text)}
    assert set(rows) == {"ex:camp/run/1/result", "ex:camp/run/2/result"}
    assert rows["ex:camp/run/1/result"]["fe_co"] == "78" and rows["ex:camp/run/1/result"]["replicate"] == "1"
    assert rows["ex:camp/run/2/result"]["run_status"] == "failed" and rows["ex:camp/run/2/result"]["fe_co__status"] == "failed_run"
    assert {x["record"]: x["reason"].split(" ")[0] for x in man["excluded"]} == {
        "ex:camp/run/1": "proposal_not_observation", "ex:camp/run/2": "proposal_not_observation"}
    assert all(r["group:campaign"] == "camp::ex:camp/design" for r in rows.values())
    assert man["rows"][0]["evidence"]["fe_co"] == ["ex:camp/dataset/runs @ run 1, column fe_co"]

    # what the convention refuses
    with pytest.raises(ScaleError, match="already has a result"):
        campaign.record_result(doc, proposal="ex:camp/run/1", result_id="ex:camp/run/1/again", recorded_at="2026-11-04T00:00:00Z")
    with pytest.raises(ScaleError, match="post hoc"):
        late = campaign.propose(doc, design="ex:camp/design", iteration=2, agent="ex:camp/agent",
                                proposed_at="2026-11-05T09:00:00+00:00", proposals=[proposal(3, -0.9)])
        campaign.record_result(late, proposal="ex:camp/run/3", result_id="ex:camp/run/3/result",
                               recorded_at="2026-11-01T00:00:00+00:00")
    with pytest.raises(ScaleError, match="states why"):
        campaign.record_result(late, proposal="ex:camp/run/3", result_id="ex:camp/run/3/result",
                               recorded_at="2026-11-06T00:00:00+00:00", status="failed")
    with pytest.raises(ScaleError, match="not a proposal"):
        campaign.record_result(doc, proposal="ex:camp/run/1/result", result_id="x:y", recorded_at="2026-11-06T00:00:00Z")
    bad = copy.deepcopy(MATRIX_PLAN)
    bad["match"] = {"label": "Prediction", "as": "test"}
    bad["steps"] = []
    with pytest.raises(ScaleError, match="never a row"):
        designmatrix.build(members(camp=doc), bad, {**MATRIX_CONTRACT, "runs": {"include": ["not_reported"]},
                                                    "features": [], "groups": [],
                                                    "outcomes": [{"name": "x", "path": "test.expected", "type": "quantity",
                                                                  "unit": "%"}]})
