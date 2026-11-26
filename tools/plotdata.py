"""Plot data: the numbers of one or several graphs, and of a linked data file, as typed chart points with their
provenance (docs/analyze.md).

``plot_data(members, spec)`` runs a plot spec's query plan over the graphs (:mod:`federation`: each graph on its own
under the schema it pins, nothing merged) and returns the panels of a chart: each point an x and a y value as reported,
the record (and result entry) it comes from, its evidence (source and locator) and its series and facet keys. The
explorer's Analyze view, ``scale graph plot-data`` and the ``scale_plot_data`` tool all read this one function.

A plot spec (``plot: 1``)::

    plot: 1
    title: Faradaic efficiency vs catalyst
    label: CatalyticTest                  # the records a point is read from (bound as the variable `rec`)
    where: [{prop: driving_forces, op: contains, value: electro}]
    x: catalyst                           # a property path of the record, or catalyst, sample, source (a category)
    y: {entries: faradaic_efficiency}     # or one result entry per point: {entries, kind, variable, target}
    series: product                       # product, sample, catalyst, source, graph, or a property path
    facet: test_kind                      # optional, the same vocabulary
    mark: point                           # point, line or bar: how the page draws it

or, instead of ``label`` and ``where``, ``plan`` (a whole query plan) with ``x``, ``y``, ``series`` and ``facet``
naming its returned paths (``var`` or ``var.prop``).

The rules:

* **As reported, never converted.** A value is plotted in the unit it states; values of one axis in two units (``%`` and
  ``mol%`` too) are two panels, never one axis. Nothing is averaged, interpolated, normalized or imputed. A plan's
  filter may not convert either (``convert: true`` is refused), and a path to a field of a value object
  (``temperature.value``) is refused: it would drop the value's unit, bound and context.
* **Contexts apart.** Each value is read under its graph's quantity contracts as ``comparison: scientific`` reads it
  (:meth:`pgraph_query.Comparison.compared_as`: its kind, the contexts its kind compares by, a unit its kind can be in).
  Values of two contexts, or of two contracts (two graphs whose schemas define the kind differently), are two panels.
  A value whose context is not stated or stated two ways, or whose unit the contracts cannot read as its kind's, is
  left out with that reason, never assumed. A value no contract covers is plotted by its unit alone, its panel marked
  ``contract: none`` (its comparability is unknown).
* **Bounds stay bounds.** A one-sided value (``bound``, a detection limit, ``lower`` or ``upper`` alone) is a bound
  point and a range (``lower`` and ``upper``) an interval; neither is a number on the axis. A reported uncertainty, a
  confidence interval (``interval_lower``/``interval_upper``) and the value's qualifier are kept as reported.
* **Observations only.** A proposal, a prediction (a Prediction, a predicted value) and a record a newer version
  REVISES are left out with their reason; the run status and the value's origin are kept on the point.
* **Everything left out is counted** by axis and reason, after what the query itself left out (each graph's status
  and coverage are kept), with the first items listed.
* **Deterministic.** Point ids are ``<graph>::<record>[#<entry>]``; panels and points are sorted; the same graphs and
  spec give the same result. The access is the intersection of the graphs' audiences.

``suggest(members)`` proposes charts from what a graph holds: per label, each outcome (conversion, selectivity,
yield, Faradaic efficiency, rates) against each condition recorded with it (temperature, pressure, potential, space
velocity, time) and against the catalyst, ranked by the values that would be plotted (read and checked as the chart
reads them), with how they split into panels.

``table_data(text, spec)`` reads a linked data file (CSV, RFC 4180) the same way: each column a unit read from its
header (``T (°C)``, ``FE [%]``), each cell a number, a bound (``<0.5``, ``>90``) or text, and each point a data row,
marked when a graph record cites it (``cited_cells``).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from typing import Any

from . import federation, pgraph_query, quantities
from .util import ScaleError, dump_canonical

PLOT_VERSION = 1
SPEC_KEYS = {"plot", "title", "description", "plan", "label", "where", "x", "y", "series", "facet", "mark", "population"}
POPULATIONS = ("observations", "all")
AXIS_KEYS = {"path", "entries", "kind", "variable", "target", "label", "via", "count"}
MARKS = ("point", "line", "bar", "box")
REC = "rec"
# what a series, a facet or a category axis may name besides a path: records a test links to, and the graph
LINKED = {
    "sample": [{"from": REC, "edge": "USES_CATALYST", "to": "CatalystSample", "as": "sample", "optional": True}],
    "catalyst": [{"from": REC, "edge": "USES_CATALYST", "to": "CatalystSample", "as": "sample", "optional": True},
                 {"from": "sample", "edge": "HAS_FORMULATION", "to": "Catalyst", "as": "catalyst", "optional": True}],
    "source": [{"from": REC, "edge": "REPORTED_IN", "to": "Source", "as": "source", "optional": True}],
}
GROUPS = tuple(LINKED) + ("product", "graph")
VALUE_FIELDS = {"value", "lower", "upper", "unit", "bound", "status", "uncertainty", "uncertainty_kind", "interval_lower",
                "interval_upper", "as_reported", "basis", "qualifier", "missing_reason"}
NUMBERED = ("present", "below_detection_limit", "below_quantification_limit")
LIMIT_STATUSES = ("below_detection_limit", "below_quantification_limit")
# what a point keeps of its value object, as reported
KEEP = ("as_reported", "status", "qualifier", "basis", "definition", "origin", "uncertainty", "uncertainty_kind",
        "uncertainty_evaluation", "interval_lower", "interval_upper", "confidence_level", "coverage_factor", "produced_by",
        "phase", "stream", "evidence_ids")
NOT_OBSERVED = ("proposed", "planned")
# what makes two x values one bucket of a count (the value as reported, never its metadata)
BUCKET_KEYS = ("type", "value", "lower", "upper", "bound", "unit", "id")
LISTED = 200
MAX_POINTS = 50000
# the quantity kinds read as conditions and as outcomes when charts are suggested (the kernel's quantity contracts)
CONDITION_KINDS = ("temperature", "pressure", "electrode_potential", "electric_potential", "space_velocity", "flow_rate",
                   "space_time", "ph", "rotational_frequency")
OUTCOME_KINDS = ("conversion", "selectivity", "product_yield", "faradaic_efficiency", "production_rate",
                 "turnover_frequency", "turnover_number", "current_density", "partial_current_density", "quantum_yield",
                 "photonic_yield", "energy_yield", "efficiency")
CONDITION_PROPS = ("duration", "time_on_stream")


# ------------------------------------------------------------------------------------------ the spec
def _scalar_of_value(path: str) -> bool:
    """Whether a path names a field of a value object (``temperature.value``, ``results.x.unit``), not the value."""
    parts = path.split(".")
    return len(parts) >= 2 and parts[-1] in VALUE_FIELDS


def _via(v: Any, where: str) -> list[dict]:
    """A path of links to follow from the record: [{edge, to, direction}], each step to a record of label ``to``."""
    if not isinstance(v, list) or not v or len(v) > 6:
        raise ScaleError(f"{where}.via is a list of one to six links: [{{edge, to, direction}}]")
    out = []
    for i, st in enumerate(v):
        if not isinstance(st, dict) or set(st) - {"edge", "to", "direction"} or not all(
                isinstance(st.get(k), str) and st[k].strip() for k in ("edge", "to")):
            raise ScaleError(f"{where}.via[{i}] is {{edge, to, direction}}: an edge type, the label it leads to, and "
                             "optionally direction out (default) or in")
        if st.get("direction", "out") not in ("out", "in"):
            raise ScaleError(f"{where}.via[{i}].direction is out or in")
        out.append(dict(st))
    return out


def _axis(a: Any, where: str, with_plan: bool) -> dict:
    if isinstance(a, str) and a.strip():
        a = {"count": True} if a.strip() == "count" and where == "y" else {"path": a.strip()}
    if not isinstance(a, dict) or set(a) - AXIS_KEYS:
        raise ScaleError(f"{where} is a path, or {{path, via}} or {{entries, kind, variable, target}} (and label)"
                         f"{', or count (y)' if where == 'y' else ''}; got {a!r}")
    if a.get("count"):
        if where != "y" or set(a) - {"count", "label"}:
            raise ScaleError("count is a y axis of its own: the records per x value (a bar), or a histogram of x")
        return dict(a)
    if "via" in a:
        if with_plan or "entries" in a:
            raise ScaleError(f"{where}.via follows links from the record: not with a plan or entries")
        a = {**a, "via": _via(a["via"], where)}
        a.setdefault("path", "")
    if ("path" in a) == ("entries" in a):
        raise ScaleError(f"{where} names a path or entries (one result entry per point), not both or neither")
    if "entries" in a and with_plan:
        raise ScaleError(f"{where}: with a plan, an axis names a returned path (bind entries with the plan's entries step)")
    for k in (AXIS_KEYS - {"via", "count"}) & set(a):
        if not (isinstance(a[k], str) and (a[k].strip() or (k == "path" and "via" in a))):
            raise ScaleError(f"{where}.{k} is text")
    if "path" in a and _scalar_of_value(a["path"]):
        raise ScaleError(f"{where}: {a['path']!r} names a field of a value object; name the value itself "
                         f"({a['path'].rsplit('.', 1)[0]!r}), so that its unit, bound and context stay with it")
    return dict(a)


def _filters(plan: dict) -> list[dict]:
    fs = list(plan.get("where") or []) + list((plan.get("match") or {}).get("where") or [])
    for st in plan.get("steps") or []:
        fs += list(st.get("where") or []) + list(st.get("edge_where") or [])
    fs += list(plan.get("edge_where") or [])
    return [f for f in fs if isinstance(f, dict)]


def load_spec(raw: Any) -> dict:
    """A plot spec, checked: unknown keys, a missing x or y, both a plan and a label (or neither), an unknown mark, a
    path to a field of a value object and a filter that converts are errors."""
    if not isinstance(raw, dict):
        raise ScaleError("a plot spec is an object {plot: 1, label (or plan), x, y, series, facet, mark}")
    bad = set(raw) - SPEC_KEYS
    if bad:
        raise ScaleError(f"a plot spec holds {sorted(SPEC_KEYS)}, not {sorted(bad)}")
    if raw.get("plot", PLOT_VERSION) != PLOT_VERSION:
        raise ScaleError(f"plot is {PLOT_VERSION} (the plot spec's version), not {raw.get('plot')!r}")
    if ("plan" in raw) == ("label" in raw):
        raise ScaleError("a plot spec names its records by label (with where) or gives a whole plan: one of them")
    with_plan = "plan" in raw
    if with_plan:
        pgraph_query.check_plan_shape(raw["plan"])
        if "where" in raw:
            raise ScaleError("with a plan, put the filters in the plan's where")
    elif not (isinstance(raw["label"], str) and raw["label"].strip()):
        raise ScaleError("label is a label of the schema, e.g. CatalyticTest")
    if raw.get("where") is not None and not (isinstance(raw["where"], list) and all(isinstance(f, dict) for f in raw["where"])):
        raise ScaleError("where is a list of filters on the record, as a plan's where")
    out = {"plot": PLOT_VERSION, **{k: raw[k] for k in ("title", "description", "plan", "label", "where") if k in raw}}
    conv = [f for f in _filters(raw.get("plan") or {"where": raw.get("where")}) if f.get("convert")]
    if conv:
        raise ScaleError("a plot filters values as reported: convert: true is not taken (a filter states its unit and "
                         "keeps the values in it)")
    for k in ("x", "y"):
        if k not in raw:
            raise ScaleError(f"a plot spec names its {k} axis")
        out[k] = _axis(raw[k], k, with_plan)
    for k in ("series", "facet"):
        if raw.get(k) is not None:
            if not (isinstance(raw[k], str) and raw[k].strip()):
                raise ScaleError(f"{k} is a path, or one of {', '.join(GROUPS)}")
            out[k] = raw[k].strip()
            if _scalar_of_value(out[k]):
                raise ScaleError(f"{k}: {out[k]!r} names a field of a value object; name the value itself")
    out["population"] = raw.get("population", "observations")
    if out["population"] not in POPULATIONS:
        raise ScaleError("population is observations (proposals, predictions and superseded records left out: the "
                         "default) or all (every record, an inventory)")
    out["mark"] = raw.get("mark", "point")
    if out["mark"] not in MARKS:
        raise ScaleError(f"mark is one of {', '.join(MARKS)}, not {out['mark']!r}")
    return out


def compile_plan(spec: dict) -> tuple[dict, dict]:
    """(the query plan a spec runs, the plan path of each role: x, y, series, facet). A spec with a plan runs it with its
    axes' paths returned and its evidence, without order_by or limit; the shorthand builds the plan from the label, the
    axes and the groups."""
    roles: dict[str, str] = {}
    if "plan" in spec:
        plan = json.loads(json.dumps(spec["plan"], default=str))
        for k in ("x", "y", "series", "facet"):
            if spec.get(k) is not None:
                roles[k] = ("count" if spec[k].get("count") else spec[k]["path"]) if k in ("x", "y") else spec[k]
        roles["record"] = plan["match"].get("as", "self")
    else:
        plan = {"match": {"label": spec["label"], "as": REC}, "steps": []}
        if spec.get("where"):
            plan["where"] = [dict(f, var=f.get("var", REC)) for f in spec["where"]]
        steps, have = plan["steps"], set()

        def link(name: str) -> None:
            for st in LINKED[name]:
                if st["as"] not in have:
                    steps.append(dict(st))
                    have.add(st["as"])

        def linked_path(p: str) -> str | None:
            """A path through a named link (sample.bet_area, catalyst.composition, source.year), or None."""
            head = p.split(".", 1)[0]
            if head in LINKED and "." in p:
                link(head)
                return p
            return None

        roles["record"] = REC
        entry_of: dict[str, str] = {}
        for k in ("x", "y"):
            a = spec[k]
            if a.get("count"):
                roles[k] = "count"
            elif "via" in a:
                prev = REC
                for i, st in enumerate(a["via"], 1):
                    var = f"{k}_via{i}"
                    steps.append({"from": prev, "edge": st["edge"], "to": st["to"], "as": var, "optional": True,
                                  **({"direction": st["direction"]} if st.get("direction") else {})})
                    prev = var
                roles[k] = f"{prev}.{a['path']}" if a["path"] else prev
            elif "entries" in a:
                var = f"{k}_entry"
                st = {"from": REC, "entries": a["entries"], "as": var}
                if a.get("kind"):
                    st["kind"] = a["kind"]
                fs = [{"prop": q, "op": "eq", "value": a[q]} for q in ("variable", "target") if a.get(q)]
                if fs:
                    st["where"] = fs
                steps.append(st)
                roles[k] = entry_of[k] = var
            elif a["path"] in LINKED:
                link(a["path"])
                roles[k] = a["path"]
            else:
                roles[k] = linked_path(a["path"]) or (a["path"] if a["path"].startswith(f"{REC}.") else f"{REC}.{a['path']}")
        for k in ("series", "facet"):
            g = spec.get(k)
            if g is None:
                continue
            if g == "product":
                ent = entry_of.get("y") or entry_of.get("x")
                if not ent:
                    raise ScaleError(f"{k} product names the product of a result entry: make y (or x) an entries axis")
                if "product" not in have:
                    steps.append({"from": ent, "field": "target", "to": "ChemicalSpecies", "as": "product", "optional": True})
                    have.add("product")
                roles[k] = "product"
            elif g in LINKED:
                link(g)
                roles[k] = g
            else:
                roles[k] = linked_path(g) or (g if g == "graph" or g.startswith(f"{REC}.") else f"{REC}.{g}")
        if not steps:
            plan.pop("steps")
    want = [p for k, p in roles.items() if p not in ("graph", "count") and k != "record"]
    have_ret = list(plan.get("return") or [])
    plan["return"] = have_ret + [p for p in want if p not in have_ret]
    plan["evidence"] = True
    if any(pgraph_query.numeric_filter(f) for f in _filters(plan)):
        plan["comparison"] = "scientific"
    plan.pop("limit", None)
    plan.pop("order_by", None)
    return plan, roles


# ------------------------------------------------------------------------------------------ typing a value
def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _unit(u: Any) -> str | None:
    return " ".join(u.split()) if isinstance(u, str) and u.strip() and not quantities.states_no_unit(u) else None


def typed(v: Any) -> tuple[dict | None, str | None]:
    """(the plotted form of one value, or None and why it is left out). The form's ``type``: value (a reported number,
    with its uncertainty), bound (``bound`` lower or upper at ``value``: a one-sided value or a detection limit), range
    (``lower`` to ``upper``), number (a bare number, no unit) or category (text); with the value object's unit and what
    it states of itself (:data:`KEEP`) as reported."""
    if isinstance(v, list):
        if len(v) != 1:
            return None, "ambiguous_multiple_values" if v else "absent"
        v = v[0]
    if v is None:
        return None, "absent"
    if isinstance(v, bool):
        return {"type": "category", "value": "true" if v else "false"}, None
    if _num(v):
        return {"type": "number", "value": v}, None
    if isinstance(v, str):
        return ({"type": "category", "value": v}, None) if v.strip() else (None, "absent")
    if not isinstance(v, dict) or "status" not in v:
        return None, "not_a_value"
    st = v.get("status")
    if st not in NUMBERED:
        if st == "not_reported":
            return None, f"not_reported:{v.get('missing_reason') or 'unstated'}"
        return None, str(st)
    keep = {k: v[k] for k in KEEP if v.get(k) is not None}
    if st in LIMIT_STATUSES:
        lim = v.get("upper") if _num(v.get("upper")) else v.get("value")
        if not _num(lim):
            return None, f"{st}_without_limit"
        out = {"type": "bound", "bound": "upper", "value": lim, **keep}
    elif v.get("bound") in ("upper", "lower") and _num(v.get("value")):
        out = {"type": "bound", "bound": v["bound"], "value": v["value"], **keep}
    elif _num(v.get("value")):
        out = {"type": "value", "value": v["value"], **keep}
    elif _num(v.get("lower")) and _num(v.get("upper")):
        out = {"type": "range", "lower": v["lower"], "upper": v["upper"], **keep}
    elif _num(v.get("lower")) or _num(v.get("upper")):
        side = "lower" if _num(v.get("lower")) else "upper"
        out = {"type": "bound", "bound": side, "value": v[side], **keep}
    else:
        x = v.get("text_value") if v.get("text_value") is not None else v.get("value")
        if isinstance(x, str) and x.strip():
            return {"type": "category", "value": x, **keep}, None
        if isinstance(v.get("boolean_value"), bool):
            return {"type": "category", "value": "true" if v["boolean_value"] else "false", **keep}, None
        return None, "not_a_number"
    unit = _unit(v.get("unit"))
    if unit is None:
        return None, "unit_unstated"
    out["unit"] = unit
    return out, None


# ------------------------------------------------------------------------------------------ plot data
def _name(g: Any, rid: Any) -> str | None:
    n = g.nodes.get(rid) if g is not None and isinstance(rid, str) else None
    if n is None:
        return None
    p = n.get("props") or {}
    for k in ("name", "title", "formula"):
        if isinstance(p.get(k), str) and p[k].strip():
            return p[k]
    return rid


def _group_text(g: Any, v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return _name(g, v) or v
    if isinstance(v, dict) and "record" in v and "path" in v:
        return f"{_name(g, v['record'])}{v['path']}"
    if isinstance(v, dict) and "status" in v:
        if v.get("as_reported"):
            return str(v["as_reported"])
        if v.get("value") is not None:
            return f"{v['value']} {v.get('unit') or ''}".strip()
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v) if not isinstance(v, (dict, list)) else json.dumps(v, sort_keys=True, ensure_ascii=False)


def _group_value(g: Any, r: dict, path: str, alias: str) -> str | None:
    """The text of a series or a facet: a bound record's name, a property's text, a value as reported."""
    if path == "graph":
        return alias
    v = r["values"].get(path) if path in r["values"] else r["bound"].get(path)
    if isinstance(v, list):
        vals = sorted({x for x in (_group_text(g, y) for y in v) if x})
        return "; ".join(vals) if vals else None
    return _group_text(g, v)


def _owner(r: dict, path: str) -> tuple[str | None, str]:
    """(the record, the entry path) a plotted value belongs to."""
    b = r["bound"].get(path.partition(".")[0])
    if isinstance(b, dict):
        return b.get("record"), b.get("path") or ""
    return (b, "") if isinstance(b, str) else (None, "")


def _not_observed(g: Any, rid: str | None) -> str | None:
    node = g.nodes.get(rid) if rid else None
    if node is None:
        return None
    p = node.get("props") or {}
    if node.get("label") == "Prediction" or p.get("origin") == "predicted":
        return "prediction_not_observation"
    if p.get("origin") == "hypothesized" or p.get("experiment_status") in NOT_OBSERVED:
        return "proposal_not_observation"
    if any(e["type"] == "REVISES" for e in g.inc.get(rid, [])):
        return "superseded"
    return None


def _axis_label(spec_axis: dict, path: str) -> str:
    if spec_axis.get("label"):
        return spec_axis["label"]
    if spec_axis.get("count"):
        return "records"
    if spec_axis.get("via"):
        hops = " → ".join(st["to"] for st in spec_axis["via"])
        return f"{hops}{' ' + _human(spec_axis['path']) if spec_axis.get('path') else ''}"
    if spec_axis.get("entries"):
        return _human(spec_axis["entries"]) + (f" ({_human(spec_axis['kind'])})" if spec_axis.get("kind") else "")
    p = path.split(".", 1)[1] if path.startswith(f"{REC}.") else path
    return "source publication year" if p == "source.year" else _human(p)


def _human(p: str) -> str:
    """A path as a reader reads it: results.methanol_yield is results: methanol yield."""
    return p.replace("results.", "results: ").replace("_", " ").replace(".", " ")


class _Reader:
    """Reads a value of one graph as a scientific comparison does (:meth:`pgraph_query.Comparison.compared_as`), its
    record-valued contexts as their identity across graphs (the identity map's class, else ``alias::id``)."""

    def __init__(self, m: dict, classes: dict):
        self.m, self.classes = m, classes
        self.cmp = pgraph_query.Comparison({}, m["spec"], m["graph"])
        self.fps: dict[str, str | None] = {}

    def read(self, owner: dict, prop: str, v: Any) -> tuple[dict | None, str | None]:
        """({kind, context, contract} or {} when no contract applies, None) or (None, why it cannot be compared)."""
        tag, why = self.cmp.compared_as(owner, prop, v)
        if why:
            return None, pgraph_query.reason_code(why) + ":" + _why_detail(why)
        if tag is None or not tag.get("kind"):
            return {}, None
        g, alias = self.m["graph"], self.m["alias"]
        ctx = {}
        for k, x in sorted((tag.get("context") or {}).items()):
            ctx[k] = (self.classes.get((alias, x)) or f"{alias}::{x}") if isinstance(x, str) and x in g.nodes else x
        extra = {k: tag[k] for k in ("shows", "converted", "denominator") if tag.get(k)}
        return {"kind": tag["kind"], "context": ctx, "contract": self.contract(tag["kind"]),
                **({"compared": extra} if extra else {})}, None

    def contract(self, kind: str) -> str | None:
        """The quantity contract of ``kind`` under this graph's schema (the kind and the contexts it compares by), as a
        digest: two graphs' values of one kind share an axis only when their contracts agree."""
        if kind not in self.fps:
            q = (self.m["spec"] or {}).get("quantities") or {}
            k = (q.get("kinds") or {}).get(kind)
            names = [c.get("context") if isinstance(c, dict) else c for c in (k or {}).get("compare_by") or []]
            ctx = {str(c): (q.get("contexts") or {}).get(c) for c in names if isinstance(c, str)}
            self.fps[kind] = None if k is None else hashlib.sha256(
                json.dumps({"kind": k, "contexts": ctx}, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return self.fps[kind]


def _why_detail(why: str) -> str:
    m = re.match(r"[a-z_]+ \((?:\S+ )?(\w+)", why)
    return m.group(1) if m else "?"


def plot_data(members: list[dict], spec: Any, identity_map: dict | None = None) -> dict:
    """The typed chart data of ``spec`` over ``members`` (:mod:`federation` members): panels of points, what was left
    out and why, each graph's status and coverage, and the access."""
    spec = load_spec(spec)
    plan, roles = compile_plan(spec)
    res = federation.run(members, plan, identity_map, limit=federation.NO_LIMIT)
    by_alias = {m["alias"]: m for m in members}
    order = {m["alias"]: i for i, m in enumerate(members)}
    used = set(res["identity"]["mappings_used"])
    classes = {(x["graph"], x["record"]): f"identity:{mp['id']}" for mp in (identity_map or {}).get("mappings") or []
               if mp["id"] in used for x in mp["records"]}
    readers: dict[str, _Reader] = {}
    points: dict[tuple, dict] = {}
    left: dict[tuple, dict] = {}
    unpaired: dict[tuple, dict] = {}
    for r in res["rows"]:
        alias = r["graph"]
        m = by_alias[alias]
        g = m["graph"]
        rd = readers.get(alias) or readers.setdefault(alias, _Reader(m, classes))
        axes = {}
        counted = r["bound"].get(roles["record"])
        counted = counted if isinstance(counted, str) else None
        for k in ("y", "x"):                         # the outcome first: a record that states none is not this chart's
            path = roles[k]
            rid, entry = _owner(r, path)
            raw = None
            if path == "count":                      # the record itself, counted once per x value
                rid, entry = counted, ""
                form, why = ({"type": "count"}, None) if counted else (None, "not_bound")
            elif path in LINKED or "." not in path and not isinstance(r["bound"].get(path), dict):
                raw = r["bound"].get(path)          # a record (a catalyst, a sample, a source) on a category axis
                rid, entry = (raw, "") if isinstance(raw, str) else (None, "")
                val = _name(g, raw) if isinstance(raw, str) else None
                form, why = ({"type": "category", "value": val, "id": f"{alias}::{raw}"}, None) if val else \
                    (None, "not_linked")
            else:
                raw = r["values"].get(path)
                form, why = typed(raw)
                if form is not None and form["type"] in ("value", "bound", "range"):
                    one = raw[0] if isinstance(raw, list) else raw
                    owner = g.records.get(rid) or {}
                    prop = entry.strip("/").split("/")[0] if entry else pgraph_query.parse_path(path.partition(".")[2])[0]
                    tag, why = rd.read(owner, prop, one)
                    if why:
                        form = None
                    else:
                        form.update(tag)
            if form is not None and spec["population"] == "observations":
                why = "prediction_not_observation" if form.get("origin") == "predicted" else \
                    next((w for w in (_not_observed(g, x) for x in dict.fromkeys([counted, rid]) if x) if w), None)
                if why:
                    form = None
            if form is None:                       # one item per point left out: its y's record and entry, the axis
                _, yrid, yent = axes.get("y") or (None, rid, entry)
                key = (alias, yrid or "", yent, k, why)
                left.setdefault(key, {"graph": alias, "record": yrid, "entry": yent or None, "axis": k, "reason": why,
                                      **({"axis_record": rid} if rid != yrid else {}),
                                      **({"as_reported": raw.get("as_reported")} if isinstance(raw, dict)
                                         and raw.get("as_reported") else {})})
                axes = None
                break
            axes[k] = (form, rid, entry)
        if axes is None:
            continue
        (fx, xrid, xent), (fy, yrid, yent) = axes["x"], axes["y"]
        if fy["type"] == "count":                    # a record counts once per x value, whatever links led there
            key = (order[alias], yrid or "", "", dump_canonical({**{k: fx.get(k) for k in BUCKET_KEYS},
                                                                 **_typing(fx)}), "")
        else:
            if xent and yent and xrid == yrid and xent != yent:   # two entries of one record: one result's pair only
                px, py = fx.get("produced_by"), fy.get("produced_by")
                if not (px and py and px == py):
                    unpaired.setdefault((alias, yrid or "", yent, "x", "unpaired_entries"), {
                        "graph": alias, "record": yrid, "entry": yent, "axis": "x", "reason": "unpaired_entries",
                        "axis_record": xrid})
                    continue
            key = (order[alias], counted or "", yrid or "", yent, xrid or "", xent)
        pt = points.get(key)
        if pt is None:
            if len(points) >= MAX_POINTS:
                raise ScaleError(f"more than {MAX_POINTS} points: narrow the records (where, or a plan) and plot again")
            node = g.nodes.get(yrid) or {}
            pt = points[key] = {"id": f"{alias}::{yrid}" + (f"#{yent}" if yent else ""), "graph": alias, "record": yrid,
                                "entry": yent or None, "name": _name(g, yrid), "label": node.get("label"),
                                "run_status": (node.get("props") or {}).get("experiment_status"),
                                "x": fx, "y": fy, "_groups": {k: set() for k in ("series", "facet") if k in roles},
                                "_ev": {}}
            if xrid != yrid or xent != yent:
                pt["x_record"], pt["x_entry"] = xrid, xent or None
        for k in pt["_groups"]:
            v = _group_value(g, r, roles[k], alias)
            if v is not None:
                pt["_groups"][k].add(v)
        for k in ("x", "y"):
            var = roles["record"] if roles[k] == "count" else roles[k].partition(".")[0]
            for e in r.get("evidence") or []:
                if e.get("var") == var:
                    item = {"axis": k, **{f: e[f] for f in sorted(e) if f not in ("quote", "note", "var", "evidence")},
                            "source_name": _name(g, e.get("source"))}
                    pt["_ev"][json.dumps(item, sort_keys=True, default=str)] = item
    plotted = {(p["graph"], p["record"] or "", p["entry"] or "") for p in points.values()}
    left.update({k: v for k, v in unpaired.items() if (k[0], k[1], k[2]) not in plotted})
    panels = _panels(list(points.values()), spec, roles, by_alias)
    left_items = [left[k] for k in sorted(left, key=lambda k: (order[k[0]], k[1], k[2], k[3], str(k[4])))]
    by_reason: dict[str, int] = {}
    for it in left_items:
        tag = f"{it['axis']}:{it['reason']}"
        by_reason[tag] = by_reason.get(tag, 0) + 1
    graphs = []
    for x in res["graphs"]:
        doc = (by_alias[x["alias"]].get("doc") or {}).get("graph") or {}
        cov = x.get("coverage") or {}
        graphs.append({**{k: x.get(k) for k in ("alias", "graph_id", "graph_sha256", "schema_version", "spec_sha256",
                                               "spec_verified", "status", "error", "row_count") if x.get(k) is not None},
                       "data_status": doc.get("data_status"),
                       "query_left_out": {**(cov.get("excluded") or {}).get("by_reason", {}),
                                          **{f"in_steps:{k}": n for k, n in (cov.get("not_evaluated_in_steps") or {})
                                             .get("by_reason", {}).items()}}})
    synthetic = [x["alias"] for x in graphs if x.get("data_status") == "synthetic_example"]
    n_points = sum(len(p["points"]) for p in panels)
    return {
        "plot": PLOT_VERSION,
        "title": spec.get("title") or f"{_axis_label(spec['y'], roles['y'])} vs {_axis_label(spec['x'], roles['x'])}",
        "status": "plotted" if n_points else ("no_points" if res["rows"] else res["status"]),
        "spec": spec,
        "plan": plan,
        "graphs": graphs,
        "partial": res["partial"],
        "identity_map_sha256": (identity_map or {}).get("sha256"),
        "panels": panels,
        "counts": {"query_rows": len(res["rows"]), "points": n_points, "panels": len(panels), "left_out": len(left_items)},
        "left_out": {"by_reason": dict(sorted(by_reason.items())), "items": left_items[:LISTED],
                     "listed": min(len(left_items), LISTED), "total": len(left_items)},
        "identity_conflicts": res["identity_conflicts"],
        "access": res["access"],
        "limitations": [
            "Values are as reported: nothing converted, normalized, averaged or imputed. Two units, two comparison "
            "contexts or two quantity contracts of one axis are two panels, never one axis.",
            "A bound is a bound point and a range an interval: neither is a number on the axis. A line joins a series' "
            "reported values in x order only to guide the eye: it states no trend the sources did not report.",
            "Proposals, predictions and superseded records are not observations and are left out; what was left out is "
            "counted by axis and reason (left_out), after what each graph's query left out (graphs[].query_left_out).",
        ] + ([f"Partial: {', '.join(res.get('partial_graphs') or [])} not evaluated."] if res["partial"] else [])
          + ([f"{', '.join(synthetic)}: synthetic examples, not evidence about the world."] if synthetic else []),
    }


def _ctx_text(by_alias: dict, ctx: dict) -> dict:
    """A value's contexts for reading: a record-valued context by its record's name."""
    out = {}
    for k, v in sorted((ctx or {}).items()):
        if isinstance(v, str) and "::" in v:
            a, _, rid = v.partition("::")
            out[k] = _name((by_alias.get(a) or {}).get("graph"), rid) or rid
        elif isinstance(v, str) and v.startswith("identity:"):
            out[k] = v.split(":", 1)[1]
        else:
            out[k] = v
    return out


def _typing(form: dict) -> dict:
    t = form["type"]
    if t in ("value", "bound", "range"):
        out = {"type": "quantity", "unit": form.get("unit"), "kind": form.get("kind"),
               "context": form.get("context") or {}, "contract": form.get("contract") or "none"}
        if out["contract"] == "none":            # no contract reads it: what it states of its basis keeps it apart
            out.update({k: form[k] for k in ("basis", "definition") if form.get(k)})
        return out
    return {"type": t}


def _panels(points: list[dict], spec: dict, roles: dict, by_alias: dict) -> list[dict]:
    panels: dict[str, dict] = {}
    for pt in points:
        tx, ty = _typing(pt["x"]), _typing(pt["y"])
        groups = pt.pop("_groups")
        ev = pt.pop("_ev")
        pt["evidence"] = [ev[k] for k in sorted(ev)]
        pt["series"] = "; ".join(sorted(groups["series"])) if groups.get("series") else None
        pt["facet"] = "; ".join(sorted(groups["facet"])) if groups.get("facet") else None
        for k in ("x", "y"):
            pt[k] = {kk: vv for kk, vv in pt[k].items() if kk not in ("kind", "context", "contract")}
        panels.setdefault(dump_canonical({"x": tx, "y": ty}), {"x": tx, "y": ty, "points": []})["points"].append(pt)
    out = []
    for i, key in enumerate(sorted(panels, key=lambda k: (-len(panels[k]["points"]), k))):
        p = panels[key]
        _distinct_names(p["points"])
        pts = sorted(p["points"], key=lambda q: (q["graph"], q["record"] or "", q["entry"] or "", q.get("x_record") or "",
                                                 q.get("x_entry") or "", q["series"] or "", q["facet"] or ""))
        axes = {}
        for k in ("x", "y"):
            t = dict(p[k])
            label = _axis_label(spec[k], roles[k])
            if spec[k].get("count") and spec.get("label") and not spec[k].get("label"):
                label = f"{spec['label']} records"
            if t.get("kind") and _human(t["kind"]) not in label:     # entries of several kinds: say which this is
                label = f"{label}: {_human(t['kind'])}"
            shown = _ctx_text(by_alias, t.get("context") or {})
            title = label + (f" ({t['unit']})" if t.get("unit") else "") + \
                (" [" + ", ".join(f"{c}: {v}" for c, v in shown.items()) + "]" if shown else "") + \
                (f" [basis: {t['basis']}]" if t.get("basis") else "")
            if t.get("type") == "quantity" and t.get("contract") == "none":
                t["comparability"] = "unknown: no quantity contract reads this value; grouped by its unit and basis only"
            if t.get("type") == "quantity":
                t["context"] = shown
                t["context_ids"] = p[k]["context"]
            axes[k] = {"path": roles[k], "label": label, "title": title, **t}
        counts = {"points": len(pts),
                  "values": sum(1 for q in pts if q["x"]["type"] not in ("bound", "range")
                                and q["y"]["type"] not in ("bound", "range")),
                  "bounds": sum(1 for q in pts if "bound" in (q["x"]["type"], q["y"]["type"])),
                  "ranges": sum(1 for q in pts if "range" in (q["x"]["type"], q["y"]["type"]))}
        panel = {"id": f"panel-{i + 1}", "title": f"{axes['y']['title']} vs {axes['x']['title']}", "x": axes["x"],
                 "y": axes["y"], "mark": spec["mark"], "series": sorted({q["series"] for q in pts if q["series"]}),
                 "facets": sorted({q["facet"] for q in pts if q["facet"]}), "counts": counts, "points": pts}
        if axes["y"]["type"] == "count":
            panel.update(_buckets(pts, axes["x"]["type"]))
        elif spec["mark"] == "box" and axes["x"]["type"] in ("category", "number") and axes["y"]["type"] == "quantity":
            panel["boxes"] = _boxes(pts)
        out.append(panel)
    return out


def _distinct_names(pts: list[dict]) -> None:
    """Two records of one name on a category axis are two categories: each name that several records share takes the
    end of its record's id (``Cu foil [sample/1]``), so that no count or box pools them."""
    for k in ("x", "y"):
        ids: dict[str, set] = {}
        for q in pts:
            if q[k].get("type") == "category" and q[k].get("id"):
                ids.setdefault(str(q[k]["value"]), set()).add(q[k]["id"])
        for q in pts:
            v = q[k]
            if v.get("type") == "category" and v.get("id") and len(ids.get(str(v["value"]), ())) > 1:
                q[k] = {**v, "value": f"{v['value']} [{v['id'].split('::', 1)[-1].rsplit('/', 2)[-2:][0]}/"
                                      f"{v['id'].rsplit('/', 1)[-1]}]"}


def _group_key(q: dict) -> tuple:
    return (q["series"] or "", q["facet"] or "")


def _buckets(pts: list[dict], xtype: str) -> dict:
    """What a count shows, counted here so that an agent reads the same numbers as the chart: per x value (a category
    or a bare number such as a year), series and facet, the records, the distinct sources that state them and the
    graphs; for a quantity, a histogram per series and facet on shared bin edges; and how many records count under more
    than one x value (a test with two samples counts once under each)."""
    out: dict[str, Any] = {}
    seen: dict[tuple, int] = {}
    for q in pts:
        seen[(q["graph"], q["record"])] = seen.get((q["graph"], q["record"]), 0) + 1
    multi = sum(1 for n in seen.values() if n > 1)
    if xtype in ("category", "number"):
        b: dict[str, dict] = {}
        for q in pts:
            key = dump_canonical([q["x"].get("value"), q["series"] or "", q["facet"] or ""])
            x = b.setdefault(key, {"x": q["x"].get("value"), "series": q["series"], "facet": q["facet"], "records": 0,
                                   "_src": set(), "_g": set()})
            x["records"] += 1
            x["_g"].add(q["graph"])
            x["_src"].update(f"{q['graph']}::{e['source']}" for e in q["evidence"] if e.get("source") and e["axis"] == "y")
        out["buckets"] = [{k: v for k, v in x.items() if not k.startswith("_")} | {"sources": len(x["_src"]),
                                                                                    "graphs": sorted(x["_g"])}
                          for _, x in sorted(b.items())]
    else:
        out["histogram"] = _histogram(pts)
    out["counted"] = {"records": len(seen), "in_more_than_one_x": multi,
                      "note": "records are counted, not sources or results: one record counts once per x value it has"}
    return out


def _nice(step: float) -> float:
    """The bin width at or above ``step``: 1, 2 or 5 times a power of ten."""
    if step <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(step))
    return next(m * e for m in (1, 2, 5, 10) if m * e >= step - 1e-12)


def _histogram(pts: list[dict], bins: int = 20) -> dict:
    """A histogram of the x values (each a reported value; bounds and ranges are never binned, counted apart), its bin
    edges chosen here so that the chart, the CSV and an agent read the same counts: about ``bins`` bins of a width of
    1, 2 or 5 times a power of ten, each [lo, hi) (the last closed), shared by every series and facet, each bin counting
    the distinct records of its series and facet in it."""
    vals = [q for q in pts if q["x"]["type"] in ("value", "number")]
    if not vals:
        return {"bins": [], "values": 0, "not_binned": len(pts), "note": "no reported values to bin"}
    lo_v, hi_v = min(q["x"]["value"] for q in vals), max(q["x"]["value"] for q in vals)
    width = _nice((hi_v - lo_v) / bins) if hi_v > lo_v else _nice(abs(lo_v) / 10 or 1)
    start = math.floor(lo_v / width) * width
    n = max(1, math.floor((hi_v - start) / width) + 1)
    recs: dict[tuple, list[set]] = {}
    for q in vals:
        i = min(n - 1, int((q["x"]["value"] - start) // width))
        recs.setdefault(_group_key(q), [set() for _ in range(n)])[i].add((q["graph"], q["record"]))
    return {"bins": [{"lo": round(start + i * width, 12), "hi": round(start + (i + 1) * width, 12), "records": len(r[i]),
                      "series": ser or None, "facet": fac or None}
                     for (ser, fac), r in sorted(recs.items()) for i in range(n)],
            "width": width, "values": len(vals), "not_binned": len(pts) - len(vals),
            "records_binned": len({(q["graph"], q["record"]) for q in vals}),
            "note": "each bin [lo, hi) counts the distinct records with a value in it; bounds and ranges are not binned "
                    "(not_binned)"}


def _quantile(xs: list[float], q: float) -> float:
    """The q-quantile of sorted values, linearly interpolated (the method most tools default to)."""
    pos = (len(xs) - 1) * q
    i = math.floor(pos)
    return xs[i] if i + 1 >= len(xs) else xs[i] + (xs[i + 1] - xs[i]) * (pos - i)


def _boxes(pts: list[dict]) -> list[dict]:
    """Box statistics per category, series and facet of the reported values: n, min, quartiles (linear
    interpolation), median, max; bounds and ranges are not in a box (n_bounds, n_ranges say how many were drawn
    apart)."""
    groups: dict[tuple, dict] = {}
    for q in pts:
        g = groups.setdefault((dump_canonical(q["x"].get("value")), *_group_key(q)),
                              {"x": q["x"].get("value"), "vals": [], "bounds": 0, "ranges": 0, "sources": set()})
        t = q["y"]["type"]
        if t == "value":
            g["vals"].append(q["y"]["value"])
        else:
            g["bounds" if t == "bound" else "ranges"] += 1
        g["sources"].update(e["source"] for e in q["evidence"] if e.get("source") and e["axis"] == "y")
    out = []
    for (_, ser, fac), g in sorted(groups.items()):
        v = sorted(g["vals"])
        box = {"x": g["x"], "series": ser or None, "facet": fac or None, "n": len(v), "n_bounds": g["bounds"],
               "n_ranges": g["ranges"], "sources": len(g["sources"]),
               "method": "quartiles by linear interpolation; whiskers from the minimum to the maximum value"}
        if v:
            box.update(min=v[0], q1=_quantile(v, 0.25), median=_quantile(v, 0.5), q3=_quantile(v, 0.75), max=v[-1])
        out.append(box)
    return out


# ------------------------------------------------------------------------------------------ CSV
CSV_AXIS = ("value", "type", "bound", "lower", "upper", "uncertainty", "uncertainty_kind", "interval_lower",
            "interval_upper", "qualifier", "basis", "origin", "unit", "as_reported")


def _fmt(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, (dict, list)):
        return json.dumps(x, sort_keys=True, ensure_ascii=False)
    return str(x)


def to_csv(result: dict) -> str:
    """One line per point: its panel, identity, series and facet, each axis as reported (its panel's axis title, kind
    and contexts; the value's type, bound, interval, uncertainty, qualifier, basis, origin and unit) and the sources
    and locators that state it. The JSON result is its manifest (graphs, access, what was left out)."""
    head = ["panel", "point", "graph", "record", "entry", "x_record", "x_entry", "name", "run_status", "series", "facet"]
    for k in ("x", "y"):
        head += [f"{k}_title", f"{k}_kind", f"{k}_context"] + [f"{k}_{c}" for c in CSV_AXIS]
    head += ["sources", "locators"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for p in result["panels"]:
        for q in p["points"]:
            line = [p["id"], q["id"], q["graph"], q["record"], q["entry"] or "", q.get("x_record") or "",
                    q.get("x_entry") or "", q.get("name") or "",
                    q.get("run_status") or "", q["series"] or "", q["facet"] or ""]
            for k in ("x", "y"):
                line += [p[k]["title"], p[k].get("kind") or "", _fmt(p[k].get("context") or "")]
                line += [_fmt(q[k].get(c)) for c in CSV_AXIS]
            srcs = sorted({f"{e.get('source_name') or e.get('source')}" + (f" ({e['source_year']})" if e.get("source_year")
                                                                            else "") for e in q["evidence"]})
            locs = sorted({f"{e['axis']}: {e.get('locator')}" for e in q["evidence"] if e.get("locator")})
            w.writerow(line + ["; ".join(srcs), "; ".join(locs)])
    return buf.getvalue()


def model_view(result: dict, offset: int = 0, page_size: int = 50) -> dict:
    """A plot result for a model to read: every panel's axes and counts, what was left out by reason, and the points
    from ``offset`` in pages (``page.next`` gives the next page's offset; ``page.truncated`` says there are more), so
    that a model never takes one page for the whole chart."""
    flat = [(p["id"], q) for p in result.get("panels") or [] for q in p["points"]]
    page = flat[offset:offset + page_size]
    return {"title": result.get("title"), "status": result.get("status"), "counts": result.get("counts"),
            "panels": [{k: p[k] for k in ("id", "title", "x", "y", "series", "facets", "counts", "buckets", "histogram",
                                          "boxes", "counted") if k in p}
                       for p in result.get("panels") or []],
            "points": [{"panel": pid, **{k: q[k] for k in ("id", "record", "entry", "x_record", "name", "x", "y", "series",
                                                           "facet") if q.get(k) is not None},
                        "evidence": sorted({(e.get("record"), e.get("source"), e.get("locator")) for e in q["evidence"]},
                                           key=str)}
                       for pid, q in page],
            "page": {"offset": offset, "returned": len(page), "total": len(flat),
                     "truncated": offset + len(page) < len(flat),
                     **({"next": offset + len(page)} if offset + len(page) < len(flat) else {})},
            "left_out": {"by_reason": (result.get("left_out") or {}).get("by_reason", {}),
                         "total": (result.get("left_out") or {}).get("total", 0)},
            "graphs": result.get("graphs"), "access": result.get("access"), "limitations": result.get("limitations")}


# ------------------------------------------------------------------------------------------ suggestions
def _role(spec: dict, record: dict, prop: str) -> str | None:
    kind = quantities.binding(spec, record, prop)[0]
    if kind in CONDITION_KINDS or prop in CONDITION_PROPS:
        return "condition"
    if kind in OUTCOME_KINDS:
        return "outcome"
    return None


def _usable(rd: _Reader, node: dict, prop: str, v: Any) -> str | None:
    """The panel a value would be plotted in (its unit, kind and contexts), or None when the chart would leave it out."""
    form, why = typed(v)
    if form is None or form["type"] not in ("value", "bound", "range") or form.get("origin") == "predicted":
        return None
    tag, why = rd.read(node, prop, v)
    if why:
        return None
    return dump_canonical({"unit": form["unit"], **(tag or {})})


COUNT_SKIP = {"name", "title", "note", "description", "statement", "origin", "evidence", "stewardship", "formula",
              "test_label", "as_reported", "quote"}


def _count_suggestions(members: list[dict], limit: int) -> list[dict]:
    """Counts worth starting from: the records of the most common labels per source year, and per a text property that
    takes a few values (2 to 25) on at least half of them (a test's kind, a claim's status)."""
    labels: dict[str, int] = {}
    years: dict[str, set] = {}
    cats: dict[tuple, dict] = {}
    for m in members:
        g = m.get("graph")
        if g is None:
            continue
        for rid in sorted(g.nodes):
            n = g.nodes[rid]
            lab = n.get("label")
            if lab in ("Source", "Institution", "Project", "Person"):
                continue
            labels[lab] = labels.get(lab, 0) + 1
            for e in g.out.get(rid, []):
                if e["type"] == "REPORTED_IN" and (g.nodes.get(e["target"]) or {}).get("props", {}).get("year") is not None:
                    years.setdefault(lab, set()).add(str(g.nodes[e["target"]]["props"]["year"]))
            for k, v in (n.get("props") or {}).items():
                if k not in COUNT_SKIP and isinstance(v, str) and v.strip() and len(v) <= 40:
                    c = cats.setdefault((lab, k), {"records": 0, "values": set()})
                    c["records"] += 1
                    c["values"].add(v)
    out = []
    for lab, _ in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(out) >= limit:
            break
        if len(years.get(lab, ())) >= 2:
            out.append({"title": f"{lab} records per source year", "records": labels[lab], "panels": 1, "label": lab,
                        "graphs": [m["alias"] for m in members],
                        "spec": {"plot": PLOT_VERSION, "label": lab, "x": "source.year", "y": "count", "mark": "bar",
                                 "title": f"{lab} records per source year"}})
        best = sorted(((k, c) for (l2, k), c in cats.items() if l2 == lab and 2 <= len(c["values"]) <= 25
                       and c["records"] * 2 >= labels[lab]), key=lambda kc: (-kc[1]["records"], kc[0]))
        if best and len(out) < limit:
            k = best[0][0]
            out.append({"title": f"{lab} records by {k.replace('_', ' ')}", "records": best[0][1]["records"], "panels": 1,
                        "label": lab, "graphs": [m["alias"] for m in members],
                        "spec": {"plot": PLOT_VERSION, "label": lab, "x": k, "y": "count", "mark": "bar",
                                 "title": f"{lab} records by {k.replace('_', ' ')}"}})
    return out[:limit]


def suggest(members: list[dict], limit: int = 12) -> dict:
    """Charts worth starting from: per label, each outcome against each condition recorded with it (and against the
    catalyst), ranked by the records whose two values the chart would plot (each read as the chart reads it: a number, a
    bound or a range, with its unit, its contexts stated), then by name; each a ready plot spec with its count and how
    many panels its units and contexts make. An outcome is a property bound to an outcome kind or a list of result
    entries (one chart for each, its products in series); a condition a property bound to a condition kind, or a
    duration. A condition with one value in every record is not suggested."""
    tallies: dict[tuple, dict] = {}
    for m in members:
        g, spec = m.get("graph"), m.get("spec")
        if m.get("status") in federation.LEAVES_GRAPH_UNREAD or spec is None:
            continue
        if g is None:
            g = m["graph"] = pgraph_query.Graph(m["doc"], spec)
        rd = _Reader(m, {})
        for rid in sorted(g.nodes):
            node = g.nodes[rid]
            props = node.get("props") or {}
            if _not_observed(g, rid):
                continue
            outs, conds = {}, {}
            for prop, v in sorted(props.items()):
                if prop in ("evidence", "stewardship"):
                    continue
                if isinstance(v, dict) and "status" in v:
                    role = _role(spec, node, prop)
                    panel = _usable(rd, node, prop, v) if role else None
                    if panel and role == "condition":
                        conds[prop] = (panel, json.dumps([v.get(k) for k in ("value", "lower", "upper", "bound", "unit")]))
                    elif panel and role == "outcome":
                        outs[("path", prop)] = [panel]
                elif isinstance(v, list) and v and all(isinstance(x, dict) and "status" in x for x in v) \
                        and any(isinstance(x.get("target"), str) for x in v):
                    ps = sorted({p for x in v if (p := _usable(rd, node, prop, x))})
                    if ps:
                        outs[("entries", prop)] = ps
            samples = sorted(f"{m['alias']}::{e['target']}" for e in g.out.get(rid, []) if e["type"] == "USES_CATALYST")
            xs = list(conds.items()) + ([("catalyst", ("category", json.dumps(samples)))] if samples else [])
            for o, ops in outs.items():
                for c, (cp, xkey) in xs:
                    t = tallies.setdefault((node.get("label"), o, c), {"records": 0, "panels": set(), "graphs": set(),
                                                                       "x": set()})
                    t["records"] += 1
                    t["graphs"].add(m["alias"])
                    t["x"].add(xkey)
                    t["panels"].update(f"{p}|{cp}" for p in ops)
    ranked = sorted(tallies.items(), key=lambda kv: (-kv[1]["records"], kv[0][0] or "", kv[0][1], kv[0][2]))
    out = []
    nice = lambda s: s.replace("_", " ")   # noqa: E731
    for (label, (how, o), c), t in ranked:
        # two records at least, and an x that varies: a chart of one condition value shows nothing against it
        if t["records"] < 2 or len(t["x"]) < 2 or len(out) >= limit:
            continue
        spec = {"plot": PLOT_VERSION, "label": label, "x": c, "y": {"entries": o} if how == "entries" else o}
        if how == "entries":
            spec["series"] = "product"
            title = f"{nice(o)} by product vs {nice(c)}"
        else:
            title = f"{nice(o)} vs {nice(c)}"
        spec["title"] = title[0].upper() + title[1:]
        out.append({"title": spec["title"], "records": t["records"], "panels": len(t["panels"]), "label": label,
                    "graphs": sorted(t["graphs"]), "spec": spec})
    out += _count_suggestions(members, max(3, limit // 3))
    return {"suggestions": out, "considered": len(tallies),
            "note": "ranked by the records whose two values a chart would plot (a number, a bound or a range, with its "
                    "unit and its contexts stated); panels: how many panels their units, contexts and contracts make"}


# ------------------------------------------------------------------------------------------ a linked data file
_HEAD_UNIT = re.compile(r"^(?P<name>.*?)\s*[\(\[](?P<unit>[^\(\)\[\]]+)[\)\]]\s*$")
_BOUND = re.compile(r"^\s*(?P<op><=|>=|<|>|≤|≥)\s*(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$")
_NUMBER = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$")
TABLE_KEYS = {"plot", "title", "x", "y", "series", "facet", "mark"}
_ROWS = re.compile(r"\brows?\s+(\d+)(?:\s*(?:-|–|to)\s*(\d+))?", re.I)


def read_table(text: str) -> tuple[list[str], list[list[str]]]:
    """A CSV text (RFC 4180: quoted fields, doubled quotes, line breaks in quotes): its header and data records."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"), newline="")))
    if not rows or not any(c.strip() for c in rows[0]):
        raise ScaleError("the file holds no header row")
    head = [h.strip() for h in rows[0]]
    if len(set(head)) != len(head) or any(not h for h in head):
        raise ScaleError("the first row is the header: one name per column, each once")
    return head, rows[1:]


def column_unit(name: str) -> tuple[str, str | None]:
    """(a column's name, the unit its header states in parentheses or brackets, or None)."""
    m = _HEAD_UNIT.match(name)
    return (m["name"].strip() or name, " ".join(m["unit"].split())) if m else (name, None)


def _cell(text: str, unit: str | None) -> tuple[dict | None, str | None]:
    t = (text or "").strip()
    if not t:
        return None, "empty"
    b = _BOUND.match(t)
    if b:
        side = "upper" if b["op"] in ("<", "<=", "≤") else "lower"
        out = {"type": "bound", "bound": side, "value": float(b["num"]), "as_reported": t}
    elif _NUMBER.match(t):
        out = {"type": "value", "value": float(t), "as_reported": t}
    else:
        return {"type": "category", "value": t}, None
    if unit:
        out["unit"] = unit
    else:
        out["type"] = "number" if out["type"] == "value" else out["type"]
    return out, None


def cited_cells(doc: dict, dataset: str) -> list[dict]:
    """The cells of a Dataset's file that the graph's records cite: an evidence item whose ``dataset_cell`` names a row
    (and column) of it is firm; one whose source is the Dataset and whose locator names a row ("row 12", "rows 3-5") is
    tentative (a locator may name another table's row). Rows count data records after the header, from 1."""
    out = []
    for r in list(doc.get("nodes") or []) + list(doc.get("links") or []):
        for e in (r.get("props") or {}).get("evidence") or []:
            if not isinstance(e, dict):
                continue
            cell = e.get("dataset_cell")
            if isinstance(cell, dict) and cell.get("dataset", e.get("source")) == dataset and isinstance(cell.get("row"), int):
                out.append({"record": r.get("id"), "row": cell["row"], "column": cell.get("column"), "firm": True,
                            "locator": e.get("locator")})
            elif e.get("source") == dataset and isinstance(e.get("locator"), str):
                for a, b in _ROWS.findall(e["locator"]):
                    lo, hi = int(a), int(b or a)
                    for row in range(lo, min(hi, lo + 1000) + 1):
                        out.append({"record": r.get("id"), "row": row, "column": None, "firm": False,
                                    "locator": e["locator"]})
    return sorted(out, key=lambda c: (c["row"], str(c["column"]), c["record"] or "", c["firm"]))


def table_csv(result: dict) -> str:
    """The points of a table chart (:func:`table_data`) as CSV: panel, row, series, facet, each axis as written (its
    title, type, bound, unit) and the graph records that cite the row."""
    head = ["panel", "row", "series", "facet"]
    for k in ("x", "y"):
        head += [f"{k}_title", f"{k}_value", f"{k}_type", f"{k}_bound", f"{k}_unit", f"{k}_as_reported"]
    head += ["cited_by"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for p in result.get("panels") or []:
        for q in p["points"]:
            line = [p["id"], q["row"], q.get("series") or "", q.get("facet") or ""]
            for k in ("x", "y"):
                line += [p[k]["title"]] + [_fmt(q[k].get(c)) for c in ("value", "type", "bound", "unit", "as_reported")]
            line.append("; ".join(f"{c['record']}{'' if c['firm'] else ' (tentative)'}" for c in q.get("cited_by") or []))
            w.writerow(line)
    return buf.getvalue()


def table_data(text: str, spec: Any, sha256: str | None = None, cited: list[dict] | None = None,
               file_sha256: str | None = None) -> dict:
    """Chart data of a linked data file (CSV): spec {x, y, series, facet, mark, title} names its columns. Each point a
    data row (``row`` from 1 after the header), each cell a number in the unit its column's header states, a bound
    (``<0.5``), a bare number (no unit stated) or text; empty cells left out by column. ``sha256``: the digest the
    graph records for the file; the result says whether the file is it (verified, mismatch, not_recorded), and rows the
    graph cites (``cited``, :func:`cited_cells`) are marked only when it is. ``file_sha256``: the digest of the file's
    bytes as read (the text's UTF-8 bytes when not given)."""
    if not isinstance(spec, dict) or set(spec) - TABLE_KEYS or not spec.get("x") or not spec.get("y"):
        raise ScaleError(f"a table plot spec holds {sorted(TABLE_KEYS)}, with x and y naming columns")
    head, rows = read_table(text)
    for k in ("x", "y", "series", "facet"):
        if spec.get(k) is not None and spec[k] not in head:
            raise ScaleError(f"{k}: no column {spec[k]!r} (the columns: {', '.join(head)})")
    digest = (file_sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest()).lower()
    check = "not_recorded" if not sha256 else ("verified" if sha256.lower() == digest else "mismatch")
    marks: dict[int, list[dict]] = {}
    if check == "verified":
        for c in cited or []:
            marks.setdefault(c["row"], []).append(c)
    ix = {h: i for i, h in enumerate(head)}
    units = {h: column_unit(h)[1] for h in head}
    points, left, typings = [], {}, {}
    numeric = {}
    for col in {spec["x"], spec["y"]}:
        cells = [_cell(r[ix[col]] if ix[col] < len(r) else "", units[col])[0] for r in rows]
        kinds = [c["type"] for c in cells if c]
        numeric[col] = sum(k != "category" for k in kinds) >= max(1, sum(k == "category" for k in kinds))
    bad_rows: dict[str, list[int]] = {}
    for n, row in enumerate(rows, 1):              # a row keeps its number in the file (blank rows count, unplotted)
        if not any(c.strip() for c in row):
            continue
        cells = {}
        for k in ("y", "x"):
            col = spec[k]
            raw = row[ix[col]] if ix[col] < len(row) else ""
            form, why = _cell(raw, units[col])
            if form is not None and form["type"] == "category" and numeric[col]:
                form, why = None, "not_a_number"
                bad_rows.setdefault(f"{k}:{col}", []).append(n)
            if form is None:
                left[f"{k}:{why}"] = left.get(f"{k}:{why}", 0) + 1
                cells = None
                break
            cells[k] = form
        if cells is None:
            continue
        pt = {"id": f"row:{n}", "row": n, "x": cells["x"], "y": cells["y"],
              "series": (row[ix[spec["series"]]].strip() or None) if spec.get("series") and ix[spec["series"]] < len(row) else None,
              "facet": (row[ix[spec["facet"]]].strip() or None) if spec.get("facet") and ix[spec["facet"]] < len(row) else None}
        if n in marks:
            pt["cited_by"] = [{k: c[k] for k in ("record", "column", "firm", "locator")} for c in marks[n]]
        points.append(pt)
        key = dump_canonical({"x": _typing(cells["x"]), "y": _typing(cells["y"])})
        typings.setdefault(key, []).append(pt)
    panels = []
    for i, key in enumerate(sorted(typings, key=lambda k: (-len(typings[k]), k))):
        t = json.loads(key)
        pts = typings[key]
        axes = {k: {"path": spec[k], "label": column_unit(spec[k])[0],
                    "title": column_unit(spec[k])[0] + (f" ({t[k]['unit']})" if t[k].get("unit") else ""), **t[k]}
                for k in ("x", "y")}
        panels.append({"id": f"panel-{i + 1}", "title": f"{axes['y']['title']} vs {axes['x']['title']}", "x": axes["x"],
                       "y": axes["y"], "mark": spec.get("mark", "point"),
                       "series": sorted({q["series"] for q in pts if q["series"]}),
                       "facets": sorted({q["facet"] for q in pts if q["facet"]}),
                       "counts": {"points": len(pts), "bounds": sum(1 for q in pts if "bound" in (q["x"]["type"], q["y"]["type"])),
                                  "cited": sum(1 for q in pts if q.get("cited_by"))},
                       "points": pts})
    return {"plot": PLOT_VERSION, "title": spec.get("title") or f"{spec['y']} vs {spec['x']}",
            "status": "plotted" if points else "no_points", "file": {"sha256": digest, "rows": len(rows),
                                                                      "columns": head, "units": units},
            "digest_check": check, "panels": panels,
            "counts": {"rows": len(rows), "points": len(points), "panels": len(panels), "left_out": sum(left.values())},
            "left_out": {"by_reason": dict(sorted(left.items())),
                         "not_a_number_rows": {k: v[:50] for k, v in sorted(bad_rows.items())}},
            "cited": {"rows": sorted(marks), "shown": check == "verified",
                      "note": "rows the graph's records cite are marked only when the file is the one the graph records "
                              "(its sha256); a locator's row is tentative, a dataset_cell firm"},
            "limitations": ["Cells are read as written: a number in its column's unit (from the header), a bound "
                            "(<, >), or text; nothing converted. The file is read where it is and sent nowhere."]}
