"""The model view of query and search results (scale.modelview; scale_query and scale_search with view: model): the
full result without its repetition, reversible. expand(view) gives back the full result, every row's values, bindings,
evidence (whose item each one is: var, record, entry, its index) and every other part of the answer; pages carry every
row, numbered evidence the same on every page; a plan of the wrong shape is an error a model can act on."""

import json

import pytest

from conftest import CATALYSIS, COMPETENCY, REPO
from scale import graphparts, modelview
from scale.api import ScaleToolkit
from scale.util import ScaleError, load_yaml, write_canonical

METHANOL = REPO / "graphs" / "methanol-synthesis-pre1966"


def chars(x) -> int:
    return len(json.dumps(x, separators=(",", ":"), ensure_ascii=False))


def pages(tk, args) -> list[dict]:
    """Every page of a query's model view, followed by page.next."""
    out, nxt = [], {}
    while True:
        p = tk.call("query", {**args, "view": "model", **nxt})          # page.next passed back whole
        assert "error" not in p, p
        out.append(p)
        if not p["page"]["next"]:
            return out
        assert p["page"]["rows"] >= 1
        nxt = p["page"]["next"]


def resolved(page: dict) -> dict:
    """Each evidence item of a page whole: its shared text and its source's fields put back."""
    back = {}
    for e, item in page["evidence"].items():
        x = {k: v for k, v in item.items() if not k.endswith("_of")}
        for f in ("quote", "transcription"):
            if f + "_of" in item:
                x[f] = page["evidence"][item[f + "_of"]][f]
        back[e] = {**x, **page["sources"].get(item.get("source"), {})}
    return back


def same_answer(tk, args) -> dict:
    """The full result, checked against its model view: whole (one page) and in pages."""
    full = tk.call("query", args)
    assert "error" not in full, full
    whole = tk.call("query", {**args, "view": "model", "page_size": 500, "page_chars": 10**8})
    assert whole["view"] == {**whole["view"], "name": "model", "of": "query", "version": modelview.VERSION}
    assert modelview.expand(whole) == full
    got = [modelview.expand(p) for p in pages(tk, args)]
    assert [r for p in got for r in p["rows"]] == full["rows"]                 # no row dropped, none twice, in order
    for p in got:
        assert {k: v for k, v in p.items() if k != "rows"} == {k: v for k, v in full.items() if k != "rows"}
    return full


def competency_runs():
    for f in sorted(COMPETENCY.glob("*.yaml")):
        q = load_yaml(f) or {}
        if not isinstance(q, dict) or not q.get("plan"):
            continue
        plans = [q["plan"]] + [{**q["plan"], **v["plan"]} for v in q.get("variants") or []]
        for doc in q.get("documents") or []:
            for i, plan in enumerate(plans):
                yield pytest.param(CATALYSIS / doc, plan, id=f"{q['id']}-{i}-{doc.rsplit('/', 1)[-1]}")


@pytest.fixture(scope="module")
def tk(tmp_path_factory):
    return ScaleToolkit(workspace=tmp_path_factory.mktemp("ws"), repo=REPO)


@pytest.fixture(scope="module")
def methanol(tmp_path_factory):
    """The methanol graph, built from its parts, and a toolkit whose working directory holds it."""
    ws = tmp_path_factory.mktemp("methanol")
    write_canonical(ws / "methanol.json", graphparts.build_folder(METHANOL))
    return ScaleToolkit(workspace=ws, repo=REPO)


@pytest.mark.parametrize("path,plan", list(competency_runs()))
def test_every_competency_question_round_trips_through_the_model_view(tk, path, plan):
    same_answer(tk, {"path": str(path), "plan": plan})


ZN_CR = {"match": {"label": "CatalyticTest", "as": "test"},
         "steps": [{"from": "test", "edge": "USES_CATALYST", "to": "CatalystSample", "as": "sample",
                    "where": [{"prop": "name", "op": "contains", "value": "chrom"}]}],
         "return": ["test.name", "sample.name", "test.temperature", "test.pressure"], "evidence": True}
SCIENTIFIC = {"match": {"label": "CatalyticTest", "as": "test"},
              "where": [{"prop": "temperature", "op": "le", "value": 300, "unit": "°C"}],
              "return": ["test.name", "test.temperature", "test.pressure"], "order_by": [{"prop": "temperature"}],
              "comparison": "scientific", "evidence": True, "limit": 400}


def test_a_real_graph_answer_loses_nothing_and_says_whose_evidence_each_item_is(methanol):
    full = same_answer(methanol, {"path": "methanol.json", "plan": ZN_CR})
    view = methanol.call("query", {"path": "methanol.json", "plan": ZN_CR, "view": "model", "page_size": 500,
                                   "page_chars": 10**8})
    assert chars(view) < 0.5 * chars(full)
    # each distinct item once; a row names its items by record and index, the record by the row's binding
    items = [it for r in full["rows"] for it in r["evidence"]]
    assert len(view["evidence"]) < len({(it["record"], it["evidence"]) for it in items}) < len(items)
    doc = json.loads((methanol.workspace / "methanol.json").read_text())
    recs = {x["id"]: x for x in doc["nodes"] + doc["links"]}
    assert view["id_prefix"] == "ex:msyn/"
    # ownership read from the view alone: item i of the record a group names (or the row binds) is that record's
    # evidence item i in the graph, field for field
    back = modelview.expand(view)
    for row in back["rows"]:
        assert row["evidence"]
        for it in row["evidence"]:
            mine = recs[it["record"]]["props"]["evidence"][it["evidence"]]
            assert {k: it[k] for k in mine} == mine
            assert it["record"] in (row["bound"].get(it["var"]), *row["links"])
    links = [g for row in view["rows"] for g in row["evidence"] if g["var"] == "USES_CATALYST"]
    assert links and all("record" not in g for g in links)            # the row's one link
    src = next(iter(view["sources"].values()))
    assert {"year", "status", "kind", "peer_reviewed", "title"} <= set(src)


def test_pages_cover_every_row_and_say_what_the_limit_left(methanol):
    full = same_answer(methanol, {"path": "methanol.json", "plan": SCIENTIFIC})
    ps = pages(methanol, {"path": "methanol.json", "plan": SCIENTIFIC})
    assert len(ps) > 1 and sum(p["page"]["rows"] for p in ps) == len(full["rows"])
    assert all(p["counts"]["rows_matched"] == full["row_count"] for p in ps)
    assert ps[0]["excluded"] and all("filter_ref" in x and "filter" not in x for x in ps[0]["excluded"])
    assert set(ps[0]["filters"]) == {x["filter_ref"] for x in ps[0]["excluded"]}
    # an evidence id names the same item on every page
    seen = {}
    for p in ps:
        for e, item in resolved(p).items():
            assert seen.setdefault(e, item) == item
    limited = methanol.call("query", {"path": "methanol.json", "plan": {**ZN_CR, "limit": 5}, "view": "model"})
    assert limited["counts"]["rows_returned"] == 5 and limited["counts"]["not_returned_by_limit"] == \
        limited["counts"]["rows_matched"] - 5
    assert "larger limit" in limited["counts"]["how_to_reach_them"]
    past = methanol.call("query", {"path": "methanol.json", "plan": {**ZN_CR, "limit": 5}, "view": "model",
                                   "offset": 9})
    assert past["rows"] == [] and past["page"]["next"] is None and "past" in past["page"]["note"]
    one = methanol.call("query", {"path": "methanol.json", "plan": ZN_CR, "view": "model", "page_size": 1})
    assert one["page"]["rows"] == 1 and one["page"]["next"]["offset"] == 1


def test_search_round_trips_through_the_model_view(methanol):
    args = {"path": "methanol.json", "query": "sulfur poisoning copper", "k": 10}
    full = methanol.call("search", args)
    if "error" in full and "numpy" in full["error"]:
        pytest.skip("vector search needs numpy")
    view = methanol.call("search", {**args, "view": "model"})
    assert modelview.expand(view) == full
    assert chars(view) < chars(full) and view["sources"]


def test_no_prefix_when_a_string_already_starts_with_the_mark():
    full = {"graph_id": "ex:a/graph", "status": "answered", "row_count": 1,
            "rows": [{"bound": {"x": "ex:a/r/1"}, "links": [], "values": {"x.name": "~ tilde"}}],
            "excluded": [], "plan": {"return": ["x.name"]}, "limitations": [], "missing_context": [],
            "coverage": {}, "access": {"level": "public"}, "extra": ["ex:a/1", "ex:a/2", "ex:a/3", "ex:a/4"]}
    view = modelview.pack_query(full)
    assert view["id_prefix"] is None and modelview.expand(view) == full
    full["rows"][0]["values"]["x.name"] = "plain"
    view = modelview.pack_query(full)
    assert view["id_prefix"] == "ex:a/" and view["rows"][0]["bound"]["x"] == "~r/1"
    assert modelview.expand(view) == full


def test_a_view_of_another_version_is_refused():
    with pytest.raises(ScaleError, match="version"):
        modelview.expand({"view": {"name": "model", "of": "query", "version": 99}})
    with pytest.raises(ScaleError, match="not a model view"):
        modelview.expand({"rows": []})


@pytest.mark.parametrize("plan,says", [
    ({"match": "Hypothesis", "return": ["h.name"]}, '"label": "Hypothesis"'),
    ({"match": {"label": "CatalyticTest"}, "steps": {"from": "self"}}, "steps is a list of objects"),
    ({"match": {"label": "CatalyticTest"}, "where": ["temperature < 300"]}, "where[0] is an object"),
    ({"match": {"label": "CatalyticTest"}, "return": "self.name"}, "return is a list"),
    ({"match": {"label": "CatalyticTest"}, "limit": "10"}, "limit is a whole number"),
    ({"match": {"label": "CatalyticTest"}, "evidence": "yes"}, "evidence is true or false"),
    ({"match": {"label": "CatalyticTest"}, "text": "copper"}, "text is an object with a query"),
    ({"match": {"label": "CatalyticTest"}, "order_by": ["temperature"]}, "order_by is a list of objects"),
    (["match"], "a plan is an object"),
])
def test_a_plan_of_the_wrong_shape_is_an_error_a_model_can_repair(methanol, plan, says):
    res = methanol.call("query", {"path": "methanol.json", "plan": plan})
    assert set(res) == {"error"} and res["error"].startswith("plan shape: ") and says in res["error"]
    assert "A plan looks like {" in res["error"]
    assert "Traceback" not in res["error"] and "AttributeError" not in res["error"]


def test_a_plan_sent_as_json_text_runs(methanol):
    plan = {"match": {"label": "Source", "as": "s"}, "return": ["s.name"], "limit": 2}
    assert methanol.call("query", {"path": "methanol.json", "plan": json.dumps(plan)})["rows"] == \
        methanol.call("query", {"path": "methanol.json", "plan": plan})["rows"]
    assert "JSON object" in methanol.call("query", {"path": "methanol.json", "plan": "{match: x"})["error"]


def test_view_and_page_arguments_are_checked(methanol):
    plan = {"match": {"label": "Source", "as": "s"}, "return": ["s.name"]}
    assert "view is full or model" in methanol.call("query", {"path": "methanol.json", "plan": plan, "view": "short"})["error"]
    assert "page_size" in methanol.call("query", {"path": "methanol.json", "plan": plan, "view": "model",
                                                  "page_size": 0})["error"]
    assert "offset" in methanol.call("query", {"path": "methanol.json", "plan": plan, "view": "model",
                                               "offset": -1})["error"]


def test_stats_and_describe_narrow_to_what_a_plan_needs(methanol):
    whole = methanol.call("stats", {"path": "methanol.json"})
    narrow = methanol.call("stats", {"path": "methanol.json", "labels": ["CatalyticTest"],
                                     "properties": ["temperature", "pressure"]})
    assert set(narrow["units_in_use"]) == {"CatalyticTest"}
    assert set(narrow["units_in_use"]["CatalyticTest"]) <= {p for p in whole["units_in_use"]["CatalyticTest"]
                                                            if p.split(".")[0].split("[")[0] in ("temperature", "pressure")}
    assert narrow["units_in_use"]["CatalyticTest"]["temperature"] == whole["units_in_use"]["CatalyticTest"]["temperature"]
    assert all("CatalyticTest" in (x["source"], x["target"]) for x in narrow["link_patterns"])
    assert narrow["nodes"] == whole["nodes"] and narrow["filtered"]["labels"] == ["CatalyticTest"]
    assert chars(narrow) < chars(whole) / 10 and "filtered" not in whole
    assert "error" in methanol.call("stats", {"path": "methanol.json", "labels": "CatalyticTest"})
    full = methanol.call("describe", {"names": ["CatalyticTest"], "domain": "catalysis"})
    lean = methanol.call("describe", {"names": ["CatalyticTest"], "domain": "catalysis", "checklist": False})
    assert "checklist" in full and "checklist" not in lean and lean["labels"] == full["labels"]
    assert "checklist_left_out" in lean


def test_a_quote_and_a_transcription_that_share_a_text_keep_their_own_fields():
    a, b = "A" * 20, "B" * 20
    full = {"status": "answered", "row_count": 2, "plan": {"return": []}, "rows": [
        {"bound": {"x": "r"}, "links": [], "values": {}, "evidence": [
            {"var": "x", "record": "r", "evidence": 0, "quote": a, "transcription": b},
            {"var": "x", "record": "r", "evidence": 1, "quote": b},
            {"var": "x", "record": "r", "evidence": 2, "transcription": a, "locator": "p. 2"}]},
        {"bound": {"x": "s"}, "links": [], "values": {}, "evidence": [
            {"var": "x", "record": "s", "evidence": 0, "quote": b}]}]}
    assert modelview.expand(modelview.pack_query(full)) == full
    pages = [modelview.pack_query(full, offset=i, page_size=1) for i in (0, 1)]
    assert [r for p in pages for r in modelview.expand(p)["rows"]] == full["rows"]
    assert resolved(pages[0])["E2"] == resolved(pages[1])["E2"] == {"quote": b}


@pytest.mark.parametrize("plan", [
    {"match": {"label": "Claim", "as": "c", "id": None}, "steps": [{"from": "c", "edge": "REPORTED_IN", "to": None,
                                                                    "as": "s", "edge_as": None}], "limit": 2},
    {"match": {"label": "Claim", "as": "c"}, "text": {}, "limit": 2},
])
def test_a_plan_that_ran_before_the_shape_check_still_runs(methanol, plan):
    assert "error" not in methanol.call("query", {"path": "methanol.json", "plan": plan})


def test_markdown_in_the_model_view_renders_the_page(methanol):
    one = methanol.call("query", {"path": "methanol.json", "plan": ZN_CR, "view": "model", "page_size": 1,
                                  "format": "markdown"})
    whole = methanol.call("query", {"path": "methanol.json", "plan": ZN_CR, "format": "markdown"})
    assert one["markdown"] and len(one["markdown"]) < len(whole["markdown"])
