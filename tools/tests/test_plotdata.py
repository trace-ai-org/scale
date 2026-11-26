"""Plot data (tools/plotdata.py; docs/analyze.md): a chart's points as reported, by panel (never two units, contexts or
contracts on one axis), bounds kept as bounds, everything left out counted, across graphs, deterministic, behind the
egress gate; the charts suggested; and a linked data file read the same way. Synthetic documents only."""

import copy
import csv
import functools
import hashlib
import io
import json

import pytest

from conftest import REPO
from scale import federation, pgraph, plotdata
from scale.api import ScaleToolkit
from scale.cli import main
from scale.egress import EgressGate
from scale.util import ScaleError, load_doc

PM = REPO / "domain/examples/scale-catal/product-measurement-examples/product-measurements.json"
RECORDED = federation.recorded_digests(REPO)
FE = "ex:pm/variable/faradaic-efficiency"
CO_FE = {"plot": 1, "label": "CatalyticTest", "x": "electrode_potential",
         "y": {"entries": "result_values", "variable": FE, "target": "ex:pm/species/co"}, "series": "sample"}
FE_ALL = {"plot": 1, "label": "CatalyticTest", "x": "electrode_potential",
          "y": {"entries": "result_values", "variable": FE}, "series": "product"}


@functools.lru_cache(maxsize=None)
def _spec(version):
    return pgraph.load_spec(pgraph.spec_for(REPO, version))


def members(**docs):
    return [federation.member(a, doc=d, spec=_spec(d["graph"]["schema_version"]), recorded=RECORDED) for a, d in docs.items()]


@pytest.fixture()
def pm():
    return copy.deepcopy(load_doc(PM))


def node(doc, rid):
    return next(n for n in doc["nodes"] if n["id"] == rid)


def entry(doc, rid, eid):
    return next(e for e in node(doc, rid)["props"]["result_values"] if e["id"] == eid)


def test_points_are_typed_as_reported_with_their_record_entry_and_evidence(pm):
    res = plotdata.plot_data(members(pm=pm), CO_FE)
    assert res["status"] == "plotted" and len(res["panels"]) == 1
    p = res["panels"][0]
    assert p["y"]["kind"] == "faradaic_efficiency" and p["y"]["unit"] == "%" and p["y"]["context"] == {"species": "CO"}
    assert p["x"]["kind"] == "electrode_potential" and p["x"]["unit"] == "V"
    assert p["x"]["context"] == {"potential_reference": "rhe"} and "[potential_reference: rhe]" in p["x"]["title"]
    pts = {q["id"]: q for q in p["points"]}
    one = pts["pm::ex:pm/test/co2rr#/result_values/co_fe_30min"]
    assert (one["y"]["type"], one["y"]["value"], one["y"]["unit"], one["y"]["produced_by"]) == \
        ("value", 26, "%", "ex:pm/assay/gc-30min")
    assert one["x"]["type"] == "value" and one["x"]["value"] == -1.0 and one["entry"] == "/result_values/co_fe_30min"
    assert one["series"] and any(e["axis"] == "y" and e.get("locator") for e in one["evidence"])
    # the repeated electrolysis's two CO results are two points, never averaged
    assert len([q for q in p["points"] if q["record"] == "ex:pm/test/co2rr-repeat"]) == 2
    assert res["access"]["level"] == "public" and res["graphs"][0]["data_status"] == "synthetic_example"
    assert any("synthetic" in x for x in res["limitations"])


def test_bounds_and_ranges_stay_bounds_and_what_is_left_out_is_counted(pm):
    entry(pm, "ex:pm/test/co2rr", "co_fe_30min").update(bound="lower", value=20)
    e = entry(pm, "ex:pm/test/co2rr", "co_fe_60min")
    del e["value"]
    e.update(lower=22, upper=26)
    entry(pm, "ex:pm/test/co2rr-repeat", "co_fe_a").update(status="not_reported", missing_reason="not_stated_in_source")
    del entry(pm, "ex:pm/test/co2rr-repeat", "co_fe_b")["unit"]
    res = plotdata.plot_data(members(pm=pm), CO_FE)
    pts = {q["entry"]: q["y"] for p in res["panels"] for q in p["points"]}
    assert pts["/result_values/co_fe_30min"]["type"] == "bound" and pts["/result_values/co_fe_30min"]["bound"] == "lower"
    assert pts["/result_values/co_fe_60min"] == {**pts["/result_values/co_fe_60min"], "type": "range", "lower": 22, "upper": 26}
    assert "value" not in pts["/result_values/co_fe_60min"]
    assert res["left_out"]["by_reason"] == {"y:not_reported:not_stated_in_source": 1, "y:unit_unstated": 1}
    assert res["panels"][0]["counts"] == {"points": 2, "values": 0, "bounds": 1, "ranges": 1}
    # a detection limit is an upper bound at the limit
    lim = plotdata.typed({"status": "below_detection_limit", "value": 0.5, "unit": "%"})[0]
    assert lim["type"] == "bound" and lim["bound"] == "upper" and lim["value"] == 0.5
    assert plotdata.typed({"status": "present", "value": 3})[1] == "unit_unstated"
    assert plotdata.typed([{"status": "present", "value": 1, "unit": "%"}] * 2)[1] == "ambiguous_multiple_values"


def test_two_units_or_two_contexts_are_two_panels_and_a_missing_context_is_left_out(pm):
    rep = node(pm, "ex:pm/test/co2rr-repeat")["props"]
    rep["electrode_potential"] = {"status": "present", "unit": "mV", "value": -1000}
    res = plotdata.plot_data(members(pm=pm), CO_FE)
    assert sorted(p["x"]["unit"] for p in res["panels"]) == ["V", "mV"]          # never one axis, never converted
    rep["electrode_potential"] = {"status": "present", "unit": "V", "value": -1.2}
    rep["reference_scale"] = "she"
    res = plotdata.plot_data(members(pm=pm), CO_FE)
    assert sorted(p["x"]["context"]["potential_reference"] for p in res["panels"]) == ["rhe", "she"]
    rep["reference_scale"] = "not_reported"
    res = plotdata.plot_data(members(pm=pm), CO_FE)
    assert len(res["panels"]) == 1 and res["left_out"]["by_reason"] == {"x:missing_context:potential_reference": 2}
    # every product's efficiency: one panel per product (its species context), each its own axis
    res = plotdata.plot_data(members(pm=load_doc(PM)), FE_ALL)
    species = [p["y"]["context"].get("species") for p in res["panels"]]
    assert len(species) == len(set(species)) > 2
    assert all(len(p["series"]) <= 1 for p in res["panels"])
    assert any(k.startswith("y:missing_context:species") for k in res["left_out"]["by_reason"])   # unassigned FE


def test_proposals_predictions_and_superseded_records_are_not_observations(pm):
    node(pm, "ex:pm/test/co2rr-repeat")["props"]["experiment_status"] = "proposed"
    res = plotdata.plot_data(members(pm=pm), CO_FE)
    assert res["left_out"]["by_reason"] == {"y:proposal_not_observation": 2}
    pm2 = copy.deepcopy(load_doc(PM))
    pm2["links"].append({"id": "ex:pm/link/revises", "type": "REVISES", "source": "ex:pm/test/co2rr-repeat",
                         "target": "ex:pm/test/co2rr", "props": {"origin": "derived"}})
    res = plotdata.plot_data(members(pm=pm2), CO_FE)
    assert res["left_out"]["by_reason"].get("y:superseded") == 2
    # a predicted value is left out of observations and plotted in an inventory (population: all), marked predicted
    pm3 = copy.deepcopy(load_doc(PM))
    entry(pm3, "ex:pm/test/co2rr", "co_fe_30min")["origin"] = "predicted"
    obs = plotdata.plot_data(members(pm=pm3), CO_FE)
    assert obs["left_out"]["by_reason"] == {"y:prediction_not_observation": 1}
    every = plotdata.plot_data(members(pm=pm3), {**CO_FE, "population": "all"})
    assert any(q["y"].get("origin") == "predicted" for p in every["panels"] for q in p["points"])


def test_a_spec_that_would_lose_a_unit_or_convert_is_refused(pm):
    for bad, msg in [({**CO_FE, "x": "electrode_potential.value"}, "field of a value object"),
                     ({**CO_FE, "where": [{"prop": "temperature", "op": "le", "value": 30, "unit": "°C", "convert": True}]},
                      "convert"),
                     ({**CO_FE, "plan": {"match": {"label": "CatalyticTest"}}}, "one of them"),
                     ({**CO_FE, "mark": "pie"}, "mark"), ({**CO_FE, "colour": "red"}, "not"),
                     ({**CO_FE, "series": "product", "y": "electrode_potential"}, "product")]:
        with pytest.raises(ScaleError, match=msg):
            plotdata.plot_data(members(pm=pm), bad)


def test_a_whole_plan_names_its_axes_by_returned_paths(pm):
    spec = {"plot": 1, "plan": {"match": {"label": "CatalyticTest", "as": "t"},
                                "steps": [{"from": "t", "entries": "result_values", "as": "fe",
                                           "where": [{"prop": "variable", "op": "eq", "value": FE}]}],
                                "limit": 1, "order_by": [{"var": "t", "prop": "temperature"}]},
            "x": "t.electrode_potential", "y": "fe", "facet": "t.test_kind"}
    res = plotdata.plot_data(members(pm=pm), spec)
    assert "limit" not in res["plan"] and "order_by" not in res["plan"] and res["plan"]["evidence"] is True
    assert res["counts"]["points"] > 1 and all(q["facet"] == "activity" for p in res["panels"] for q in p["points"])


def test_across_graphs_points_say_their_graph_and_contexts_join_only_by_the_identity_map(pm):
    priv = json.loads(json.dumps(load_doc(PM)).replace("ex:pm/", "lab:pm/"))
    priv["graph"]["stewardship"] = {"access": "project", "project": "lab:project/p1"}
    one ={**CO_FE, "series": "graph", "y": {"entries": "result_values", "kind": "faradaic_efficiency"}}
    res = plotdata.plot_data(members(public=pm, private=priv), one)
    assert {q["graph"] for p in res["panels"] for q in p["points"]} == {"public", "private"}
    assert res["access"]["level"] == "project"
    co = [p for p in res["panels"] if p["y"]["context"].get("species") == "CO"]
    assert len(co) == 2                                       # CO of two graphs is one species only by a map
    imap = federation.load_identity_map({"identity_map": 1, "mappings": [
        {"id": "species-co", "records": [{"graph": "public", "record": "ex:pm/species/co"},
                                         {"graph": "private", "record": "lab:pm/species/co"}],
         "evidence": [{"basis": "the same formula (CO) and charge (0) in both graphs"}]}]})
    res = plotdata.plot_data(members(public=pm, private=priv), one, imap)
    co = [p for p in res["panels"] if p["y"]["context_ids"].get("species") == "identity:species-co"]
    assert len(co) == 1 and {q["series"] for q in co[0]["points"]} == {"public", "private"}
    assert res["identity_map_sha256"] == imap["sha256"]


def test_plot_data_is_deterministic_and_its_csv_carries_provenance(pm):
    a = plotdata.plot_data(members(pm=pm), FE_ALL)
    b = plotdata.plot_data(members(pm=copy.deepcopy(pm)), FE_ALL)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    rows = list(csv.DictReader(io.StringIO(plotdata.to_csv(a))))
    assert len(rows) == a["counts"]["points"]
    assert {"point", "record", "entry", "x_unit", "y_unit", "y_type", "y_kind", "y_context", "sources", "locators"} <= set(rows[0])
    assert all(r["point"].startswith("pm::") and r["y_unit"] == "%" for r in rows)
    view = plotdata.model_view(a, 0, 3)
    assert len(view["points"]) == 3 and view["page"]["truncated"] and view["page"]["next"] == 3


def test_suggested_charts_are_ready_specs_ranked_by_what_they_would_plot(pm):
    s = plotdata.suggest(members(pm=pm))
    assert s["suggestions"]
    for x in s["suggestions"]:
        res = plotdata.plot_data(members(pm=pm), x["spec"])
        assert res["counts"]["points"] >= 1, x["title"]
    recs = [x["records"] for x in s["suggestions"] if x["spec"]["y"] != "count"]
    assert recs == sorted(recs, reverse=True)
    assert s == plotdata.suggest(members(pm=copy.deepcopy(pm)))


def test_the_tool_is_behind_the_egress_gate_and_the_cli_writes_the_points(pm, tmp_path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    priv = copy.deepcopy(pm)
    priv["graph"]["stewardship"] = {"access": "project", "project": "ex:project/p1"}
    (ws / "p.json").write_text(json.dumps(priv))
    (ws / "pub.json").write_text(json.dumps(pm))
    kit = ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate("anthropic", []))
    for args in ({"path": "p.json", "spec": CO_FE}, {"document": priv, "spec": CO_FE},
                 {"graphs": [{"alias": "a", "path": "pub.json"}, {"alias": "b", "path": "p.json"}], "spec": CO_FE},
                 {"path": "p.json", "suggest": True}):
        out = kit.call("plot_data", args)
        assert "egress denied" in out.get("error", ""), out
    ok = kit.call("plot_data", {"path": "pub.json", "spec": CO_FE, "format": "csv"})
    assert ok["status"] == "plotted" and ok["csv"].startswith("panel,point,")
    assert kit.call("plot_data", {"path": "pub.json", "spec": CO_FE, "view": "model", "page_size": 1})["page"]["returned"] == 1
    assert kit.call("plot_data", {"path": "pub.json", "suggest": True})["suggestions"]
    assert "error" in kit.call("plot_data", {"path": "pub.json"})
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(CO_FE))
    assert main(["graph", "plot-data", str(ws / "pub.json"), str(spec), "--csv", str(tmp_path / "p.csv"),
                 "--out", str(tmp_path / "p.json")]) == 0
    assert json.loads((tmp_path / "p.json").read_text())["status"] == "plotted"
    assert (tmp_path / "p.csv").read_text().startswith("panel,point,")
    capsys.readouterr()
    assert main(["graph", "plot-data", "--graph", f"a={ws / 'pub.json'}", "--graph", f"b={ws / 'p.json'}", str(spec)]) == 0
    both = json.loads(capsys.readouterr().out)
    assert {g["alias"] for g in both["graphs"]} == {"a", "b"} and both["access"]["level"] == "project"


TABLE = ('time (min),"FE CO [%]",phase,note\n'
         '0,12.5,gas,"first, quoted"\n'
         '10,<0.5,gas,"line\nbreak"\n'
         '20,>90,gas,\n'
         '30,,liquid,empty y\n'
         '40,abc,liquid,text\n')


def test_a_linked_data_file_is_read_with_its_units_bounds_digest_and_cited_rows():
    head, rows = plotdata.read_table(TABLE)
    assert head == ["time (min)", "FE CO [%]", "phase", "note"] and len(rows) == 5 and rows[1][3] == "line\nbreak"
    assert plotdata.column_unit("FE CO [%]") == ("FE CO", "%") and plotdata.column_unit("phase") == ("phase", None)
    digest = hashlib.sha256(TABLE.encode()).hexdigest()
    doc = {"nodes": [{"id": "ex:ds", "label": "Dataset", "props": {"sha256": digest}},
                     {"id": "ex:t", "label": "CatalyticTest", "props": {"evidence": [
                         {"source": "ex:ds", "locator": "rows 2-3", "quote": "x"},
                         {"source": "ex:src", "locator": "row 1, another table", "quote": "y"},
                         {"source": "ex:ds", "locator": "Table S1", "dataset_cell": {"row": 1, "column": "FE CO [%]"}}]}}],
           "links": []}
    cited = plotdata.cited_cells(doc, "ex:ds")
    assert [(c["row"], c["firm"]) for c in cited] == [(1, True), (2, False), (3, False)]
    spec = {"x": "time (min)", "y": "FE CO [%]", "series": "phase"}
    res = plotdata.table_data(TABLE, spec, digest, cited)
    assert res["digest_check"] == "verified" and res["cited"]["rows"] == [1, 2, 3]
    pts = {q["row"]: q for p in res["panels"] for q in p["points"]}
    assert pts[1]["y"] == {"type": "value", "value": 12.5, "as_reported": "12.5", "unit": "%"}
    assert pts[2]["y"]["type"] == "bound" and pts[2]["y"]["bound"] == "upper" and pts[3]["y"]["bound"] == "lower"
    assert pts[1]["cited_by"][0]["firm"] is True and pts[2]["cited_by"][0]["firm"] is False
    assert pts[1]["x"]["unit"] == "min" and 4 not in pts
    # text in a column of numbers is reported with its row, never plotted as a category of results
    assert res["left_out"]["by_reason"] == {"y:empty": 1, "y:not_a_number": 1}
    assert res["left_out"]["not_a_number_rows"] == {"y:FE CO [%]": [5]}
    assert {p["y"]["type"] for p in res["panels"]} == {"quantity"}
    wrong = plotdata.table_data(TABLE, spec, "0" * 64, cited)
    assert wrong["digest_check"] == "mismatch" and not any(q.get("cited_by") for p in wrong["panels"] for q in p["points"])
    assert plotdata.table_data(TABLE, spec)["digest_check"] == "not_recorded"
    with pytest.raises(ScaleError, match="no column"):
        plotdata.table_data(TABLE, {"x": "time", "y": "FE CO [%]"})


def test_the_cli_plots_a_data_file_against_its_dataset_record(tmp_path):
    f = tmp_path / "data.csv"
    f.write_bytes(TABLE.encode())
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"nodes": [{"id": "ex:ds", "label": "Dataset", "props": {
        "sha256": hashlib.sha256(TABLE.encode()).hexdigest()}}], "links": []}))
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"x": "time (min)", "y": "FE CO [%]"}))
    out = tmp_path / "out.json"
    assert main(["data", "plot-table", str(f), str(spec), "--graph-file", str(g), "--dataset", "ex:ds", "--out", str(out),
                 "--csv", str(tmp_path / "t.csv")]) == 0
    assert json.loads(out.read_text())["digest_check"] == "verified"
    assert (tmp_path / "t.csv").read_text().startswith("panel,row,series,")


def test_counts_histograms_and_boxes_are_counted_here_once_per_record(pm):
    # records per category: one per record, whatever links led there; the buckets say their sources
    res = plotdata.plot_data(members(pm=pm), {"label": "CatalyticTest", "x": "test_kind", "y": "count"})
    (p,) = res["panels"]
    assert p["y"]["type"] == "count" and p["y"]["title"] == "CatalyticTest records"
    assert sum(b["records"] for b in p["buckets"]) == p["counted"]["records"] == len({q["record"] for q in p["points"]})
    # a histogram's bins are chosen here, each counting distinct records; a bound is never binned
    node(pm, "ex:pm/test/co2rr-repeat")["props"]["temperature"] = {"status": "present", "unit": "K", "value": 298,
                                                                     "bound": "upper"}
    res = plotdata.plot_data(members(pm=pm), {"label": "CatalyticTest", "x": "temperature", "y": "count"})
    k = next(p for p in res["panels"] if p["x"].get("unit") == "K")
    assert k["histogram"]["not_binned"] == 1
    assert sum(b["records"] for b in k["histogram"]["bins"]) == k["histogram"]["values"]
    assert all(b["hi"] > b["lo"] for b in k["histogram"]["bins"])
    # box statistics per category, from reported values only
    res = plotdata.plot_data(members(pm=load_doc(PM)), {**FE_ALL, "x": "test_kind", "mark": "box", "series": None})
    box = next(b for p in res["panels"] for b in p.get("boxes") or [] if b["n"] >= 2)
    assert box["min"] <= box["q1"] <= box["median"] <= box["q3"] <= box["max"]
    assert plotdata._quantile([1, 2, 3, 4], 0.5) == 2.5


def test_a_value_through_links_and_a_population_policy(pm):
    via = {"via": [{"edge": "USES_CATALYST", "to": "CatalystSample"}], "path": ""}
    res = plotdata.plot_data(members(pm=pm), {**CO_FE, "x": via})
    assert res["counts"]["points"] >= 1 and all(p["x"]["type"] == "category" for p in res["panels"])
    assert {"from": "rec", "edge": "USES_CATALYST", "to": "CatalystSample", "as": "x_via1", "optional": True} in \
        res["plan"]["steps"]
    back = plotdata.load_spec({**CO_FE, "x": {"via": [{"edge": "USES_CATALYST", "to": "CatalyticTest", "direction": "in"}]}})
    assert back["x"]["via"][0]["direction"] == "in"
    with pytest.raises(ScaleError, match="via"):
        plotdata.load_spec({**CO_FE, "x": {"via": [{"edge": "E"}]}})
    # a proposal is left out of observations, counted in an inventory
    node(pm, "ex:pm/test/co2rr-repeat")["props"]["experiment_status"] = "proposed"
    spec = {"label": "CatalyticTest", "x": "test_kind", "y": "count"}
    obs = plotdata.plot_data(members(pm=pm), spec)
    every = plotdata.plot_data(members(pm=pm), {**spec, "population": "all"})
    assert every["counts"]["points"] == obs["counts"]["points"] + 1


def test_two_entries_of_one_record_pair_only_through_one_assay(pm):
    both = {"plot": 1, "label": "CatalyticTest",
            "x": {"entries": "result_values", "variable": FE, "target": "ex:pm/species/h2"},
            "y": {"entries": "result_values", "variable": FE, "target": "ex:pm/species/co"}}
    res = plotdata.plot_data(members(pm=pm), both)
    pts = [q for p in res["panels"] for q in p["points"]]
    # the 60-min GC measured CO and H2: one pair; the 30-min CO has no H2 of its assay and is never paired by position
    assert [(q["entry"], q["x_entry"]) for q in pts] == [("/result_values/co_fe_60min", "/result_values/h2_fe")]
    assert res["left_out"]["by_reason"].get("x:unpaired_entries") == 1


def test_a_data_files_rows_keep_their_numbers_and_a_bare_bound_is_plotted():
    res = plotdata.table_data("a,b\n1,<2\n\n3,4\n", {"x": "a", "y": "b"})
    assert sorted(q["row"] for p in res["panels"] for q in p["points"]) == [1, 3]   # a blank row keeps the next its number
    pt = next(q for p in res["panels"] for q in p["points"] if q["row"] == 1)
    assert pt["y"]["type"] == "bound" and "unit" not in pt["y"]
    assert plotdata.table_csv(res).startswith("panel,row,")


def test_review_regressions_selection_grouping_identity_and_the_gate(pm, tmp_path):
    # a numeric filter selects as a scientific comparison does: a potential on no stated scale is not -1.0 V vs RHE
    node(pm, "ex:pm/test/co2rr-repeat")["props"]["reference_scale"] = "not_reported"
    flt = {**CO_FE, "where": [{"prop": "electrode_potential", "op": "eq", "value": -1.0, "unit": "V"}]}
    res = plotdata.plot_data(members(pm=pm), flt)
    assert res["plan"]["comparison"] == "scientific"
    assert {q["record"] for p in res["panels"] for q in p["points"]} == {"ex:pm/test/co2rr"}
    # a count keeps kinds and contexts apart: equal CO and H2 efficiencies of one test are two panels' records
    pm2 = copy.deepcopy(load_doc(PM))
    entry(pm2, "ex:pm/test/co2rr", "h2_fe")["value"] = 24
    res = plotdata.plot_data(members(pm=pm2), {"plot": 1, "label": "CatalyticTest", "y": "count",
                                               "x": {"entries": "result_values", "variable": FE}})
    species = {p["x"]["context"].get("species") for p in res["panels"]}
    assert {"CO", "H2"} <= species
    # boxes and histograms aggregate within each facet, never across
    res = plotdata.plot_data(members(pm=load_doc(PM)), {**FE_ALL, "x": "test_kind", "mark": "box", "series": None,
                                                         "facet": "product"})
    assert all(b["facet"] for p in res["panels"] for b in p.get("boxes") or [])
    # a result plotted in one pair is not also listed as unpaired
    same = {"plot": 1, "label": "CatalyticTest", "x": {"entries": "result_values", "variable": FE, "target": "ex:pm/species/co"},
            "y": {"entries": "result_values", "variable": FE, "target": "ex:pm/species/co"}}
    res = plotdata.plot_data(members(pm=load_doc(PM)), same)
    plotted = {(q["record"], q["entry"]) for p in res["panels"] for q in p["points"]}
    assert not any((i["record"], i["entry"]) in plotted for i in res["left_out"]["items"])
    # two records of one name are two categories
    pm3 = copy.deepcopy(load_doc(PM))
    for n in pm3["nodes"]:
        if n["label"] == "CatalystSample":
            n["props"]["name"] = "Cu foil"
    res = plotdata.plot_data(members(pm=pm3), {"label": "CatalyticTest", "x": "sample", "y": "count"})
    names = [b["x"] for p in res["panels"] for b in p["buckets"]]
    assert len(names) == len(set(names)) and all(x.startswith("Cu foil") for x in names)
    # the tool refuses a document and a path together (the gate checks one of them)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "p.json").write_text(json.dumps(pm))
    kit = ScaleToolkit(workspace=ws, repo=REPO)
    assert "not both" in kit.call("plot_data", {"document": pm, "path": "p.json", "spec": CO_FE})["error"]
