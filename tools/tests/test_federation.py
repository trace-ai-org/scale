"""Querying across knowledge graphs without merging them (tools/federation.py; docs/querying.md, "Querying across
graphs"). Every document here is a public synthetic example, or a copy of one made in the test."""

import copy
import functools
import json

import pytest

from conftest import HEAD_VERSION, REPO
from scale import federation, pgraph, pgraph_query
from scale.api import ScaleToolkit, template
from scale.cli import main
from scale.util import ScaleError, load_doc

X = REPO / "domain/examples/scale-catal"
PM = X / "product-measurement-examples/product-measurements.json"        # pins 0.7.1
SDL = X / "agentic-examples/self-driving-campaign.json"                    # pins 0.7.2
RECORDED = federation.recorded_digests(REPO)

FE_PLAN = {"question": "Which Faradaic efficiencies were measured, and of which product?",
           "match": {"label": "CatalyticTest", "as": "test"},
           "steps": [{"from": "test", "entries": "result_values", "kind": "faradaic_efficiency", "as": "fe"},
                     {"from": "fe", "field": "target", "to": "ChemicalSpecies", "as": "product", "optional": True}],
           "return": ["test.name", "fe", "product.formula"], "evidence": True}
CO_MAP = {"identity_map": 1, "mappings": [
    {"id": "species-co", "records": [{"graph": "public", "record": "ex:pm/species/co"},
                                     {"graph": "private", "record": "lab:pm/species/co"}],
     "evidence": [{"basis": "the same formula (CO) and charge (0) in both graphs"}]}]}


def private_copy(doc: dict, prefix: str = "lab:pm/", project: str = "lab:project/p1") -> dict:
    """The public example as a project's own graph: its ids under another prefix, its access the project's."""
    d = json.loads(json.dumps(doc).replace("ex:pm/", prefix))
    d["graph"]["stewardship"] = {"access": "project", "project": project}
    return d


@functools.lru_cache(maxsize=None)
def _spec(version):
    return pgraph.load_spec(pgraph.spec_for(REPO, version))


def spec_of(doc):
    return _spec(doc["graph"].get("schema_version"))


def members(**docs):
    out = []
    for alias, doc in docs.items():
        if doc is None:
            out.append(federation.member(alias, status="unavailable", error="no such file"))
            continue
        try:
            spec = spec_of(doc)
        except ScaleError as exc:
            out.append(federation.member(alias, doc=doc, status="unsupported_version", error=str(exc)))
            continue
        out.append(federation.member(alias, doc=doc, spec=spec, recorded=RECORDED))
    return out


@pytest.fixture(scope="module")
def public():
    return load_doc(PM)


def test_each_graph_answers_on_its_own_and_every_row_says_its_graph(public):
    priv = private_copy(public)
    res = federation.run(members(public=public, private=priv), FE_PLAN)
    one = {a: pgraph_query.run(d, spec_of(d), {**FE_PLAN, "limit": 1000}) for a, d in (("public", public), ("private", priv))}
    assert res["federated"] and res["status"] == "answered" and not res["partial"]
    assert res["row_count"] == sum(r["row_count"] for r in one.values()) == len(res["rows"])
    for r in res["rows"]:
        assert r["graph"] in ("public", "private") and r["row_key"].startswith(r["graph"] + "/")
        assert r["graph_sha256"] == one[r["graph"]]["graph_sha256"]
        prefix = "ex:pm/" if r["graph"] == "public" else "lab:pm/"
        assert r["bound"]["test"].startswith(prefix)                   # ids as each graph states them, never rewritten
    for g in res["graphs"]:
        assert g["schema_version"] == "0.7.1" and g["spec_verified"] is True and g["evaluated"]
        assert g["spec_sha256"] == pgraph.spec_sha256(spec_of(public)) == RECORDED["0.7.1"]
        assert g["coverage"]["ran"] and g["graph_sha256"]
    # a row's evidence is its own graph's
    assert all(e["source"].startswith("ex:pm/") for r in res["rows"] if r["graph"] == "public" for e in r["evidence"])


def test_shared_reference_records_join_only_through_the_identity_map(public):
    priv = private_copy(public)
    plan = {**FE_PLAN, "join": [{"var": "product", "as": "same_product"}]}
    res = federation.run(members(public=public, private=priv), plan)
    assert res["joins"][0]["matches"] == [] and res["identity"]["mappings_used"] == []
    assert any("equal ids are never joined" in x for x in res["limitations"])
    res = federation.run(members(public=public, private=priv), plan, federation.load_identity_map(CO_MAP))
    j = res["joins"][0]
    assert [m["identity"] for m in j["matches"]] == ["species-co"]
    m = j["matches"][0]["graphs"]
    assert set(m) == {"public", "private"} and {x["record"] for x in m["public"]} == {"ex:pm/species/co"} \
        and {x["record"] for x in m["private"]} == {"lab:pm/species/co"}
    keys = {r["row_key"] for r in res["rows"]}
    assert {x["row_key"] for xs in m.values() for x in xs} <= keys         # joined rows are rows of the union, not new ones
    assert res["identity"]["map_sha256"] and res["identity"]["mappings_used"] == ["species-co"]
    # the same ids in two graphs, unmapped, are counted and never joined
    same = private_copy(public, prefix="ex:pm/")
    res = federation.run(members(public=public, private=same), plan)
    assert res["joins"][0]["matches"] == [] and "ex:pm/species/co" in res["joins"][0]["equal_ids_not_mapped"]
    assert res["identity_conflicts"] == [] and res["identity"]["ids_in_several_graphs"] == len(public["nodes"])


def test_one_id_with_two_definitions_is_an_identity_conflict_never_one_picked(public):
    other = private_copy(public, prefix="ex:pm/")
    sp = next(n for n in other["nodes"] if n["id"] == "ex:pm/species/co")
    sp["props"]["charge"] = 1
    res = federation.run(members(public=public, private=other), FE_PLAN)
    c = [x for x in res["identity_conflicts"] if x["kind"] == "equal_id"]
    assert [(x["record"], x["differ_in"]) for x in c] == [("ex:pm/species/co", ["charge"])]
    flagged = [r for r in res["rows"] if r.get("identity_conflict")]
    assert flagged and {r["graph"] for r in flagged} == {"public", "private"}         # both, neither taken for the other
    assert all(r["identity_conflict"] == ["ex:pm/species/co"] for r in flagged)
    # a mapping whose records disagree is refused, and nothing joins through it
    priv = private_copy(public)
    next(n for n in priv["nodes"] if n["id"] == "lab:pm/species/co")["props"]["formula"] = "CO2"
    res = federation.run(members(public=public, private=priv), {**FE_PLAN, "join": [{"var": "product"}]},
                         federation.load_identity_map(CO_MAP))
    assert res["identity"]["refused"] == [{"mapping": "species-co", "reason": "identity_conflict: formula"}]
    assert res["joins"][0]["matches"] == [] and res["identity_conflicts"][0]["kind"] == "mapping"
    # a species whose charge one graph does not state is not identified with one of charge 0
    del next(n for n in priv["nodes"] if n["id"] == "lab:pm/species/co")["props"]["charge"]
    next(n for n in priv["nodes"] if n["id"] == "lab:pm/species/co")["props"]["formula"] = "CO"
    res = federation.run(members(public=public, private=priv), FE_PLAN, federation.load_identity_map(CO_MAP))
    assert res["identity"]["refused"][0]["reason"] == "identity_conflict: charge"


def test_a_mapping_needs_evidence_a_reference_label_and_known_records(public):
    priv = private_copy(public)
    bad = copy.deepcopy(CO_MAP)
    bad["mappings"][0]["evidence"] = []
    bad["mappings"].append({"id": "sample", "records": [{"graph": "public", "record": "ex:pm/sample/cu-foil"},
                                                        {"graph": "private", "record": "lab:pm/sample/cu-foil"}],
                            "evidence": [{"note": "same name"}]})
    bad["mappings"].append({"id": "ghost", "records": [{"graph": "public", "record": "ex:pm/species/h2"},
                                                       {"graph": "private", "record": "lab:pm/species/none"}],
                            "evidence": [{"note": "x"}]})
    res = federation.run(members(public=public, private=priv), FE_PLAN, federation.load_identity_map(bad))
    why = {r["mapping"]: r["reason"].split(":")[0] for r in res["identity"]["refused"]}
    assert why == {"species-co": "no_evidence", "sample": "not_a_reference_record", "ghost": "no_such_record"}
    for broken in ({"identity_map": 2, "mappings": []}, {"identity_map": 1, "mappings": [], "trust": "all"},
                   {"identity_map": 1, "mappings": [{"id": "a", "records": [{"graph": "public", "record": "x"}]}]}):
        with pytest.raises(ScaleError):
            federation.load_identity_map(broken)


def test_mixed_schema_versions_are_each_read_under_their_own(public):
    sdl, head = load_doc(SDL), template("catalysis", HEAD_VERSION)
    old = copy.deepcopy(head)
    old["graph"]["schema_version"] = "0.0.9"                          # a version this repository never held
    plan = {"match": {"label": "Source", "as": "s"}, "return": ["s.name"]}
    res = federation.run(members(a=public, b=sdl, c=head, d=old), plan)
    g = {x["alias"]: x for x in res["graphs"]}
    assert [g[a]["schema_version"] for a in "abcd"] == ["0.7.1", "0.7.2", HEAD_VERSION, "0.0.9"]
    assert len({g[a]["spec_sha256"] for a in "abc"}) == 3 and all(g[a]["spec_verified"] for a in "abc")
    assert g["d"]["status"] == "unsupported_version" and "not a version this repository holds" in g["d"]["error"]
    assert res["partial"] and res["partial_graphs"] == ["d"] and res["status"] == "answered"
    assert {r["graph"] for r in res["rows"]} == {"a", "b", "c"}
    # a caller who binds a graph to another digest gets spec_mismatch for that graph, never a read under it
    m = federation.member("a", doc=public, spec=spec_of(public), expect_spec="0" * 64, recorded=RECORDED)
    res = federation.run([m] + members(c=head), plan)
    assert res["graphs"][0]["status"] == "spec_mismatch" and res["partial_graphs"] == ["a"]
    # a property one version does not declare is that graph's schema_gap; the others answer
    gap = {"match": {"label": "CatalyticTest", "as": "t", "where": [{"prop": "result_values", "op": "exists"}]},
           "return": ["t.name"]}
    res = federation.run(members(a=public, b=sdl), gap)
    assert all(x["status"] in ("answered", "no_matching_record", "schema_gap") for x in res["graphs"])


def test_partial_coverage_is_marked_and_an_absence_there_is_unknown(public):
    res = federation.run(members(public=public, gone=None), FE_PLAN)
    assert res["partial"] and res["partial_graphs"] == ["gone"] and res["status"] == "answered"
    assert any(x.startswith("Partial answer: gone was not evaluated") for x in res["limitations"])
    assert res["access"]["level"] == "institution"                   # an unread graph fails closed
    res = federation.run(members(gone=None), FE_PLAN)
    assert res["status"] == "unavailable" and res["rows"] == []
    nothing = {**FE_PLAN, "match": {"label": "CatalyticTest", "as": "test", "where": [{"prop": "name", "op": "eq", "value": "none"}]}}
    res = federation.run(members(public=public, private=private_copy(public)), nothing)
    assert res["status"] == "no_matching_record" and not res["partial"]


def test_scientific_groups_span_graphs_only_on_compatible_contexts(public):
    priv = private_copy(public)
    plan = {**FE_PLAN, "comparison": "scientific", "order_by": [{"var": "fe", "prop": "value", "desc": True}]}
    res = federation.run(members(public=public, private=priv), plan)
    assert all(len(g["graphs"]) == 1 for g in res["comparison"]["groups"])      # species unmapped: never one context
    res = federation.run(members(public=public, private=priv), plan, federation.load_identity_map(CO_MAP))
    spans = [g for g in res["comparison"]["groups"] if len(g["graphs"]) > 1]
    assert len(spans) == 1 and spans[0]["graphs"] == ["public", "private"]
    assert spans[0]["compared_on"]["fe.value"]["context"] == {"species": {"identity": "species-co"}}
    co = [r for r in res["rows"] if r["comparison_group"] == spans[0]["group"]]
    vals = [r["values"]["fe"]["value"] for r in co]
    assert vals == sorted(vals, reverse=True) and {r["graph"] for r in co} == {"public", "private"}
    assert all("graph_comparison_group" in r for r in res["rows"])
    # a graph whose schema's contract for the kind differs never shares a group with it
    other = copy.deepcopy(spec_of(priv))
    other["quantities"]["kinds"]["faradaic_efficiency"]["compare_by"] = ["species", "potential_reference"]
    ms = members(public=public) + [federation.member("private", doc=priv, spec=other)]
    res = federation.run(ms, plan, federation.load_identity_map(CO_MAP))
    assert all(len(g["graphs"]) == 1 for g in res["comparison"]["groups"])


def test_ranking_and_limit_apply_after_federation_with_truncation_reported(public):
    priv = private_copy(public)
    for n in priv["nodes"]:                          # the private results ten points higher: they rank first
        for v in (n["props"].get("result_values") or []):
            if isinstance(v.get("value"), (int, float)):
                v["value"] += 10
    plan = {**FE_PLAN, "order_by": [{"var": "fe", "prop": "value", "desc": True}], "limit": 3}
    res = federation.run(members(public=public, private=priv), plan)
    assert res["truncated"] and len(res["rows"]) == 3 and res["row_count"] > 3
    assert res["coverage"]["limit"] == {"rows_in": res["row_count"], "rows_out": 3}
    vals = [r["values"]["fe"].get("value") for r in res["rows"]]
    assert vals == sorted(vals, reverse=True) and res["rows"][0]["graph"] == "private"
    assert any("truncated" in x for x in res["limitations"])
    assert all("fe.value" not in r["values"] for r in res["rows"])        # a ranking path read, never added to a row
    again = federation.run(members(public=public, private=priv), plan)
    assert json.dumps(again["rows"], sort_keys=True) == json.dumps(res["rows"], sort_keys=True)     # deterministic


def test_the_answer_access_is_the_intersection_of_the_graphs_audiences(public):
    p1, p2 = private_copy(public), private_copy(public, prefix="lab2:pm/", project="lab2:project/p2")
    res = federation.run(members(public=public, private=p1), FE_PLAN)
    assert res["access"]["level"] == "project" and res["access"]["basis"] == "intersection"
    assert res["access"]["requires"] == [{"graph": "private", "level": "project", "project": "lab:project/p1"}]
    assert not res["access"]["disjoint"] and res["access"]["graphs"]["public"]["level"] == "public"
    res = federation.run(members(one=p1, two=p2), FE_PLAN)
    acc = res["access"]
    assert acc["level"] == "project" and acc["disjoint"] is True
    assert {r["project"] for r in acc["requires"]} == {"lab:project/p1", "lab2:project/p2"}
    assert "never relabelled to the more open" in acc["note"]
    # a membership stated in one graph does not open another's records; a plan that finds nothing is restricted too
    nothing = {**FE_PLAN, "match": {"label": "CatalyticTest", "as": "test", "where": [{"prop": "name", "op": "eq", "value": "none"}]}}
    res = federation.run(members(public=public, one=p1, two=p2), nothing)
    assert res["status"] == "no_matching_record" and res["access"]["level"] == "project" and res["access"]["disjoint"]
    inst = private_copy(public, prefix="lab3:pm/")
    inst["graph"]["stewardship"] = {"access": "institution", "contributed_by": ["lab3:org/a"]}
    res = federation.run(members(one=p1, three=inst), FE_PLAN)
    assert res["access"]["level"] == "institution" and res["access"]["disjoint"]
    assert federation.run(members(public=public), FE_PLAN)["access"]["level"] == "public"


def test_plan_errors_reach_the_caller():
    doc = load_doc(PM)
    with pytest.raises(ScaleError, match="join var"):
        federation.run(members(public=doc, other=private_copy(doc)), {**FE_PLAN, "join": [{"var": "nobody"}]})
    with pytest.raises(ScaleError, match="at least two graphs"):
        federation.run(members(public=doc), {**FE_PLAN, "join": [{"var": "product"}]})
    with pytest.raises(ScaleError, match="match.label"):
        federation.run(members(public=doc, other=private_copy(doc)), {"return": ["x"]})
    with pytest.raises(ScaleError, match="alias"):
        federation.parse_graph_args(["public=a.json", "public=b.json"])
    with pytest.raises(ScaleError):
        federation.parse_graph_args(["a.json"])


def test_toolkit_cli_and_search_federate_and_single_graph_calls_are_unchanged(tmp_path, capsys):
    doc = load_doc(PM)
    for n in doc["nodes"]:                           # quotes to search (the example cites locators only)
        for e in n["props"].get("evidence") or []:
            e["quote"] = f"the Faradaic efficiency of {n['props']['name']}"
    (tmp_path / "public.json").write_text(json.dumps(doc))
    (tmp_path / "private.json").write_text(json.dumps(private_copy(doc)))
    (tmp_path / "map.json").write_text(json.dumps(CO_MAP))
    (tmp_path / "plan.json").write_text(json.dumps({**FE_PLAN, "join": [{"var": "product"}]}))
    tk = ScaleToolkit(workspace=tmp_path, repo=REPO)
    single = tk.call("query", {"path": "public.json", "plan": FE_PLAN})
    assert "federated" not in single and single["rows"] and "graph" not in single["rows"][0]
    res = tk.call("query", {"graphs": [{"alias": "public", "path": "public.json"},
                                       {"alias": "private", "path": "private.json"}],
                            "plan": {**FE_PLAN, "join": [{"var": "product"}]}, "identity_map": "map.json",
                            "format": "markdown"})
    assert res["federated"] and res["joins"][0]["matches"] and "| public | answered |" in res["markdown"]
    assert tk.call("query", {"graphs": [{"alias": "a", "path": "public.json"}], "path": "x.json", "plan": FE_PLAN})["error"]
    assert "error" in tk.call("query", {"path": "public.json", "plan": FE_PLAN, "identity_map": "map.json"})
    hits = tk.call("search", {"graphs": [{"alias": "public", "path": "public.json"},
                                         {"alias": "private", "path": "private.json"}], "query": "Faradaic efficiency", "k": 4})
    assert hits["federated"] and len(hits["hits"]) == 4 and hits["truncated"]
    assert [h["rank_in_graph"] for h in hits["hits"]] == [1, 1, 2, 2] and [h["graph"] for h in hits["hits"]][:2] == ["public", "private"]
    assert hits["access"]["level"] == "project"
    assert main(["--repo", str(REPO), "graph", "query", "--graph", f"public={tmp_path / 'public.json'}",
                 "--graph", f"private={tmp_path / 'private.json'}", "--identity-map", str(tmp_path / "map.json"),
                 str(tmp_path / "plan.json")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["joins"][0]["matches"][0]["identity"] == "species-co" and out["access"]["level"] == "project"
    assert main(["--repo", str(REPO), "graph", "search", "--graph", f"public={tmp_path / 'public.json'}",
                 "Faradaic efficiency", "-k", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["hits"][0]["graph"] == "public"
    assert main(["--repo", str(REPO), "graph", "query", str(tmp_path / "public.json"), str(tmp_path / "plan.json")]) == 0
    assert "federated" not in json.loads(capsys.readouterr().out)
