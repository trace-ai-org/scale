"""The design bridge: a typed table for experiment-design agents, from a query plan and a contract (docs/querying.md,
"The design matrix").

``scale data matrix --graph ALIAS=FILE ... --plan PLAN --contract CONTRACT --out DIR`` runs the plan over the graphs
(:mod:`federation`: each graph on its own, a tagged union) and writes ``DIR/matrix.csv`` and
``DIR/matrix.manifest.json`` (canonical JSON). The contract (YAML or JSON, :func:`load_contract`) says what a row is and
what each column is::

    contract: 1
    name: co2rr-fe
    row: {var: test}                       # one row per record (or result entry) the variable binds
    runs: {include: [completed, failed]}   # run statuses kept; a failed run stays, with its reason
    features:
      - {name: potential, path: test.electrode_potential, type: quantity, unit: V,
         context: {potential_reference: rhe}, bounds: {lower: -1.5, upper: 0}}
      - {name: catalyst, path: catalyst.name, type: category}
    outcomes:
      - {name: fe_co, path: fe, type: quantity, unit: "%", objective: maximize, select: {product.formula: CO}}
    groups:
      - {name: specimen, path: sample}     # keys for leakage-safe splits

The rules:

* **Explicit row identity.** A row is one record (or one result entry) of the row variable in one graph
  (``row_id`` = ``<alias>::<record>[#<entry path>]``), with its run status, replicate and interval when the contract
  names them. Several plan rows of one identity are a pivot: each column must then bind one record or entry among them
  (``select`` names it), else the matrix is refused as an ambiguous pivot. Nothing is averaged, nothing multiplied out.
* **Typed cells.** A value in another unit than the column's, or of a scientific comparison's other context, refuses
  the matrix (incompatible outcomes or features: nothing is converted or normalized). Every cell has a status:
  ``present``, ``range``, ``censored_lower``/``censored_upper`` (a one-sided bound, its number in ``__lower`` or
  ``__upper``), ``below_detection_limit``, ``not_reported:<reason>``, ``not_measured``, ``not_applicable``,
  ``not_bound`` (the plan bound no value), ``failed_run`` (a failed or aborted run's absent outcome),
  ``prediction_not_observation`` (a predicted value, never an observation) or ``unit_unstated``. No cell is imputed.
* **Observations only.** A record that is a proposal (origin hypothesized, experiment_status proposed or planned), a
  record a newer version REVISES (the result recorded for a proposal supersedes it) and a run whose status the
  contract does not include are left out, each with its reason in the manifest. A Prediction is never a row.
* **Provenance.** The manifest names every graph (alias, graph digest, schema version, spec digest, whether verified),
  the plan's, the contract's and the identity map's digests, the table's digest, each row's plan rows and the evidence
  of each cell (source and locator), the records the query left out by reason, the grouping keys for splits and the
  answer's access: the intersection of the graphs' audiences. The table and the manifest go only where that access
  admits.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

from . import federation
from .util import ScaleError, dump_canonical, load_doc, write_canonical

CONTRACT_VERSION = 1
NAME = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
TYPES = ("quantity", "number", "category", "boolean", "text", "record")
OBJECTIVES = ("maximize", "minimize", "target", "none")
NOT_OBSERVED = ("proposed", "planned")
FAILED = ("failed", "aborted")
COLUMN_KEYS = {"name", "path", "type", "unit", "variable", "context", "categories", "bounds", "objective", "target",
               "fidelity", "select", "description"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_contract(data: dict | str | Path) -> dict:
    """A matrix contract, checked: unknown keys, a name that is not snake_case or used twice, a quantity without its
    unit, an objective on a feature or a type it does not know are errors."""
    raw = load_doc(data) if isinstance(data, (str, Path)) else data
    if not isinstance(raw, dict):
        raise ScaleError("a matrix contract is an object {contract: 1, row, features, outcomes, ...}")
    bad = set(raw) - {"contract", "name", "description", "row", "runs", "features", "outcomes", "groups"}
    if bad:
        raise ScaleError(f"a matrix contract holds contract, name, description, row, runs, features, outcomes and groups, "
                         f"not {sorted(bad)}")
    if raw.get("contract") != CONTRACT_VERSION:
        raise ScaleError(f"contract is {CONTRACT_VERSION} (the contract format's version), not {raw.get('contract')!r}")
    row = raw.get("row")
    if not isinstance(row, dict) or not isinstance(row.get("var"), str) or set(row) - {"var", "replicate", "interval"}:
        raise ScaleError("row is {var, replicate, interval}: the plan variable one row stands for, and optionally the "
                         "paths of its replicate and its interval")
    runs = raw.get("runs") or {}
    if not isinstance(runs, dict) or set(runs) - {"include"} or not all(isinstance(x, str) for x in runs.get("include") or []):
        raise ScaleError("runs is {include: [statuses]}: the experiment statuses kept (default: completed)")
    if set(runs.get("include") or []) & set(NOT_OBSERVED):
        raise ScaleError("runs.include never holds proposed or planned: a proposal is not an observation")
    names = set()
    for role in ("features", "outcomes"):
        cols = raw.get(role) or []
        if not isinstance(cols, list):
            raise ScaleError(f"{role} is a list of columns")
        for c in cols:
            if not isinstance(c, dict) or set(c) - COLUMN_KEYS:
                raise ScaleError(f"a column holds {sorted(COLUMN_KEYS)}; got {c!r}")
            if not isinstance(c.get("name"), str) or not NAME.match(c["name"]) or c["name"] in names:
                raise ScaleError(f"column name {c.get('name')!r}: snake_case, each name once")
            names.add(c["name"])
            if not isinstance(c.get("path"), str) or not c["path"]:
                raise ScaleError(f"column {c['name']}: path names a plan variable or variable.property")
            t = c.get("type", "quantity")
            if t not in TYPES:
                raise ScaleError(f"column {c['name']}: type is one of {TYPES}")
            if t == "quantity" and not (isinstance(c.get("unit"), str) and c["unit"].strip()):
                raise ScaleError(f"column {c['name']}: a quantity states its unit (values in another are refused, never "
                                 "converted)")
            if "objective" in c and (role != "outcomes" or c["objective"] not in OBJECTIVES):
                raise ScaleError(f"column {c['name']}: an objective ({', '.join(OBJECTIVES)}) belongs to an outcome")
            if c.get("objective") == "target" and "target" not in c:
                raise ScaleError(f"column {c['name']}: objective target states its target")
            if "select" in c and not (isinstance(c["select"], dict) and c["select"]):
                raise ScaleError(f"column {c['name']}: select is {{path: value}}, naming the one plan row a pivot reads")
            if "context" in c and not isinstance(c["context"], dict):
                raise ScaleError(f"column {c['name']}: context is a map of the quantity contexts it requires")
    if not raw.get("outcomes"):
        raise ScaleError("a matrix contract names at least one outcome")
    for g in raw.get("groups") or []:
        if not isinstance(g, dict) or set(g) != {"name", "path"} or not NAME.match(str(g.get("name"))) or g["name"] in names:
            raise ScaleError(f"a group is {{name, path}} with a snake_case name of its own; got {g!r}")
        names.add(g["name"])
    return raw


def _paths(contract: dict) -> list[str]:
    out = []
    row = contract["row"]
    out += [row[k] for k in ("replicate", "interval") if row.get(k)]
    for c in (contract.get("features") or []) + (contract.get("outcomes") or []):
        out.append(c["path"])
        out += list((c.get("select") or {}).keys())
    out += [g["path"] for g in contract.get("groups") or []]
    return list(dict.fromkeys(out))


def _plan_with(plan: dict, contract: dict) -> dict:
    p = json.loads(json.dumps(plan, default=str))
    have = list(p.get("return") or [])
    p["return"] = have + [x for x in _paths(contract) if x not in have]
    p["evidence"] = True
    p.pop("limit", None)
    return p


def _value(r: dict, path: str) -> Any:
    if path in r["values"]:
        return r["values"][path]
    return r["bound"].get(path)


def _bound_of(r: dict, path: str) -> Any:
    var = path.partition(".")[0]
    b = r["bound"].get(var)
    return json.dumps(b, sort_keys=True)


def _selected(r: dict, select: dict | None) -> bool:
    for path, want in (select or {}).items():
        got = _value(r, path)
        if isinstance(got, dict) and "value" in got:
            got = got.get("value")
        if got != want and str(got) != str(want):
            return False
    return True


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _cell(v: Any, col: dict, run_status: str, problems: list[str], where: str) -> dict:
    """One typed cell: {status, value, lower, upper, uncertainty, uncertainty_kind}; a unit other than the column's
    is a problem (the matrix is refused), never converted."""
    t = col.get("type", "quantity")
    if isinstance(v, list):
        if len(v) != 1:
            problems.append(f"{where}: {col['path']} gives {len(v)} values; bind one (an entries step, or a path to one "
                            "entry)")
            return {"status": "ambiguous"}
        v = v[0]
    if v is None:
        return {"status": "failed_run" if run_status in FAILED else "not_bound"}
    if t == "record":
        return {"status": "present", "value": v if isinstance(v, str) else json.dumps(v, sort_keys=True)}
    if isinstance(v, dict) and "status" in v:
        if v.get("origin") == "predicted":
            return {"status": "prediction_not_observation"}
        st = v.get("status")
        extra = {k: v[k] for k in ("uncertainty", "uncertainty_kind") if k in v}
        if st != "present":
            if st == "not_reported":
                return {"status": f"not_reported:{v.get('missing_reason') or 'unstated'}"}
            out = {"status": st}
            if st in ("below_detection_limit", "below_quantification_limit"):
                lim = v.get("upper") if _number(v.get("upper")) else v.get("value")
                if _number(lim):                     # a limit is a number in a unit: checked like a value
                    unit = v.get("unit")
                    if t == "quantity" and (not isinstance(unit, str) or not unit.strip()):
                        return {"status": "unit_unstated"}
                    if t == "quantity" and " ".join(unit.split()) != " ".join(col["unit"].split()):
                        problems.append(f"{where}: {col['name']}'s limit is in {unit!r}, the contract's unit is "
                                        f"{col['unit']!r} (never converted)")
                        return {"status": "incompatible_unit"}
                    out["upper"] = lim
            return out
        if t == "quantity":
            unit = v.get("unit")
            if not isinstance(unit, str) or not unit.strip():
                return {"status": "unit_unstated"}
            if " ".join(unit.split()) != " ".join(col["unit"].split()):
                problems.append(f"{where}: {col['name']} is in {unit!r}, the contract's unit is {col['unit']!r} (never "
                                "converted)")
                return {"status": "incompatible_unit"}
        if t in ("quantity", "number"):
            if v.get("bound") in ("upper", "lower") and _number(v.get("value")):
                return {"status": f"censored_{v['bound']}", v["bound"]: v["value"], **extra}
            if _number(v.get("value")):
                return {"status": "present", "value": v["value"], **extra}
            if _number(v.get("lower")) or _number(v.get("upper")):
                return {"status": "range", **{k: v[k] for k in ("lower", "upper") if _number(v.get(k))}, **extra}
            return {"status": "not_a_number"}
        if t == "category" or t == "text":
            x = v.get("text_value") if v.get("text_value") is not None else v.get("value")
            return {"status": "present", "value": x} if x is not None else {"status": "not_a_category"}
        if t == "boolean":
            return {"status": "present", "value": v["boolean_value"]} if isinstance(v.get("boolean_value"), bool) \
                else {"status": "not_a_boolean"}
    if t == "quantity":
        return {"status": "unit_unstated"}                 # a bare number states no unit
    if t == "number":
        return {"status": "present", "value": v} if _number(v) else {"status": "not_a_number"}
    if t == "boolean":
        return {"status": "present", "value": v} if isinstance(v, bool) else {"status": "not_a_boolean"}
    return {"status": "present", "value": v if isinstance(v, str) else json.dumps(v, sort_keys=True)}


def _owner(g: Any, r: dict, path: str) -> dict | None:
    """The record a cell's value belongs to: the node its variable binds (a result entry's record)."""
    b = r["bound"].get(path.partition(".")[0])
    rid = b.get("record") if isinstance(b, dict) else b
    return g.nodes.get(rid) if isinstance(rid, str) else None


def _same_context(got: Any, want: Any) -> bool:
    """A context the contract asks for: a record-valued one (``alias::id``, ``identity:<mapping>``) exactly, any other
    as the comparison reads it (its value before a qualifier)."""
    got, want = str(got), str(want)
    if "::" in got or got.startswith("identity:"):
        return got == want
    return got == want or got.split(":")[0] == want


def _read_context(m: dict, r: dict, path: str, v: Any, classes: dict, cache: dict) -> dict | None:
    """The kind and contexts of a cell's value under its quantity contract in its own graph's schema (as a scientific
    comparison reads it: :func:`quantities.read_value`), whatever the plan ranked; a record-valued context (a species)
    as its identity across graphs, else (alias, id). None when no contract applies."""
    from . import pgraph_query, quantities
    if not isinstance(v, dict):
        return None
    g, spec = m["graph"], m["spec"]
    if m["alias"] not in cache:
        cache[m["alias"]] = (quantities.contracts(spec), quantities.variable_kinds(g.doc))
    c, variables = cache[m["alias"]]
    if c is None:
        return None
    var, _, prop = path.partition(".")
    b = r["bound"].get(var)
    if isinstance(b, dict):                          # a bound result entry: the property it is an entry of
        if prop not in ("", "value") or not isinstance(b.get("path"), str):
            return None
        owner, top = g.nodes.get(b["record"]), b["path"].strip("/").split("/")[0]
    else:
        if not prop or not isinstance(b, str):
            return None
        owner, top = g.nodes.get(b), pgraph_query.parse_path(prop)[0]
    if owner is None:
        return None
    got = quantities.read_value(c, spec, owner, top, v, g.records, variables)
    if got is None:
        return None
    ctx = {}
    for name, x in sorted(got.contexts.items()):
        if x.contradiction:
            ctx[name] = {"incompatible_context": x.contradiction}
        elif x.missing and not x.unstated:
            ctx[name] = {"missing_context": x.missing}
        elif isinstance(x.value, str) and x.value in g.nodes:
            ctx[name] = classes.get((m["alias"], x.value)) or f"{m['alias']}::{x.value}"
        else:
            ctx[name] = x.value
    return {"kind": got.kind, "context": ctx}


def build(members: list[dict], plan: dict, contract: dict, identity_map: dict | None = None) -> tuple[str, dict]:
    """The matrix of ``plan`` over ``members`` under ``contract``: the CSV text and the manifest. Refuses (ScaleError) an
    ambiguous pivot, an outcome or feature in another unit or comparison context than its column, and a Prediction
    as a row."""
    contract = load_contract(contract)
    run_plan = _plan_with(plan, contract)
    res = federation.run(members, run_plan, identity_map, limit=federation.NO_LIMIT)
    by_alias = {m["alias"]: m for m in members}
    used = set(res["identity"]["mappings_used"])
    classes = {(x["graph"], x["record"]): f"identity:{mp['id']}" for mp in (identity_map or {}).get("mappings") or []
               if mp["id"] in used for x in mp["records"]}
    rv =contract["row"]["var"]
    include = set((contract.get("runs") or {}).get("include") or ["completed"])
    cols = [dict(c, role="feature") for c in contract.get("features") or []] + \
           [dict(c, role="outcome") for c in contract.get("outcomes") or []]
    problems: list[str] = []
    excluded: list[dict] = []
    ident: dict[tuple, dict] = {}
    for r in res["rows"]:
        b = r["bound"].get(rv)
        if b is None:
            excluded.append({"graph": r["graph"], "row_key": r["row_key"], "reason": "row_variable_unbound"})
            continue
        rec, entry = (b["record"], b["path"]) if isinstance(b, dict) else (b, "")
        if not isinstance(rec, str):
            raise ScaleError(f"row.var {rv!r} binds a link; a matrix row is a record or a result entry")
        key = (r["graph"], rec, entry)
        ident.setdefault(key, {"rows": []})["rows"].append(r)
    rows_out, manifest_rows = [], []
    missing: dict[str, dict[str, int]] = {c["name"]: {} for c in cols}
    contexts: dict[str, dict[str, str]] = {c["name"]: {} for c in cols}
    read_cache: dict = {}
    for key in sorted(ident, key=lambda k: ([m["alias"] for m in members].index(k[0]), k[1], k[2])):
        alias, rec, entry = key
        g = by_alias[alias]["graph"]
        node = g.nodes.get(rec) or {}
        p = node.get("props") or {}
        rid = f"{alias}::{rec}" + (f"#{entry}" if entry else "")
        if node.get("label") == "Prediction":
            raise ScaleError(f"{rid} is a Prediction: a prediction is never a row of observations")
        status = p.get("experiment_status") or "not_reported"
        why = None
        if p.get("origin") == "hypothesized" or status in NOT_OBSERVED:
            why = "proposal_not_observation"
        elif any(e["type"] == "REVISES" for e in g.inc.get(rec, [])):
            why = "superseded (a newer version REVISES it)"
        elif status not in include and not (status == "not_reported" and "not_reported" in include):
            why = f"run_status_not_included ({status})"
        if why:
            excluded.append({"graph": alias, "row_id": rid, "record": rec, "reason": why})
            continue
        prs = ident[key]["rows"]
        out = {"row_id": rid, "graph": alias, "record": rec, "entry": entry, "run_status": status}
        for part in ("replicate", "interval"):
            path = contract["row"].get(part)
            if path:
                vals = {json.dumps(_value(r, path), sort_keys=True) for r in prs}
                if len(vals) > 1:
                    problems.append(f"{rid}: its {part} ({path}) takes {len(vals)} values: an ambiguous pivot")
                out[part] = json.loads(next(iter(vals))) if vals else None
        cells, cell_ev = {}, {}
        for c in cols:
            cand = [r for r in prs if _selected(r, c.get("select"))]
            seen = {}
            for r in cand:
                seen.setdefault((_bound_of(r, c["path"]), json.dumps(_value(r, c["path"]), sort_keys=True)), r)
            if len(seen) > 1:
                problems.append(f"{rid}: column {c['name']} ({c['path']}) binds {len(seen)} different records or values "
                                f"among the {len(prs)} plan rows of this row: an ambiguous pivot (name one with select, or "
                                "make the row the entry); never averaged")
                continue
            r = next(iter(seen.values()), None)
            raw = _value(r, c["path"]) if r else None
            owner = _owner(by_alias[alias]["graph"], r, c["path"]) if r else None
            if owner is not None and (owner.get("label") == "Prediction"
                                      or (owner.get("props") or {}).get("origin") == "hypothesized"):
                cell = {"status": "prediction_not_observation"}       # what a prediction or proposal expects
            else:
                cell = _cell(raw, c, status, problems, rid)
            has_number = any(k in cell for k in ("value", "lower", "upper"))
            tag = _read_context(by_alias[alias], r, c["path"], raw[0] if isinstance(raw, list) and len(raw) == 1 else raw,
                                classes, read_cache) if r and c.get("type", "quantity") == "quantity" and has_number else None
            want = c.get("context") or {}
            if tag is not None:
                bad = {k: v for k, v in tag["context"].items() if isinstance(v, dict)}
                if bad:                                   # a context not stated, or stated two ways: never assumed
                    name, why = next(iter(sorted(bad.items())))
                    cell = {"status": f"{next(iter(why))}:{name}"}
                elif any(k not in tag["context"] or not _same_context(tag["context"][k], v) for k, v in want.items()):
                    problems.append(f"{rid}: {c['name']} is a {tag['kind']} on {tag['context']}, not on the contract's "
                                    f"{want} (never converted)")
                else:
                    unit = cell.get("unit") or c.get("unit")
                    contexts[c["name"]][json.dumps({"unit": unit, **tag}, sort_keys=True)] = rid
            elif want and has_number:
                problems.append(f"{rid}: column {c['name']} states a context, but no quantity contract says which "
                                f"{', '.join(sorted(want))} its value is on")
            cells[c["name"]] = cell
            missing[c["name"]][cell["status"]] = missing[c["name"]].get(cell["status"], 0) + 1
            if r is not None:
                var = c["path"].partition(".")[0]          # the items the query attributes to the column's variable
                cell_ev[c["name"]] = sorted({f"{e.get('source')} @ {e.get('locator')}" for e in r.get("evidence") or []
                                             if e.get("var") == var})
        groups = {}
        for gk in contract.get("groups") or []:
            vals = {json.dumps(_value(r, gk["path"]), sort_keys=True) for r in prs}
            if len(vals) > 1:
                problems.append(f"{rid}: group {gk['name']} ({gk['path']}) takes {len(vals)} values: an ambiguous pivot")
            v = json.loads(next(iter(vals))) if vals else None
            if isinstance(v, str) and v in g.nodes:          # a record: its identity across graphs, else (alias, id)
                v = classes.get((alias, v)) or f"{alias}::{v}"
            groups[gk["name"]] = v
        out.update(cells=cells, groups=groups)
        rows_out.append(out)
        manifest_rows.append({"row_id": rid, "graph": alias, "record": rec, **({"entry": entry} if entry else {}),
                              "plan_rows": [r["row_key"] for r in prs], "evidence": cell_ev,
                              **({"ambiguous_repetition": sorted({x for r in prs for xs in (r.get("ambiguous_repetition")
                                                                                            or {}).values() for x in xs})}
                                 if any(r.get("ambiguous_repetition") for r in prs) else {})})
    for c in cols:
        if len(contexts[c["name"]]) > 1:
            problems.append(f"{c['role']} {c['name']} spans {len(contexts[c['name']])} comparison contexts "
                            f"({'; '.join(sorted(contexts[c['name']]))}): incompatible; state the context in the "
                            "contract and the plan, or make one matrix per context")
    if problems:
        raise ScaleError("the matrix is refused:\n- " + "\n- ".join(problems[:50])
                         + (f"\n- ... and {len(problems) - 50} more" if len(problems) > 50 else ""))
    csv_text = _csv(rows_out, cols, contract)
    manifest = {
        "matrix": 1,
        "contract": {"name": contract.get("name"), "sha256": _sha(dump_canonical(contract).encode())},
        "plan": {"question": plan.get("question"), "sha256": _sha(dump_canonical(plan).encode())},
        "identity_map_sha256": (identity_map or {}).get("sha256"),
        "graphs": [{k: g.get(k) for k in ("alias", "graph_id", "graph_sha256", "schema_version", "spec_sha256",
                                         "spec_verified", "status")} for g in res["graphs"]],
        "partial": res["partial"],
        "row_identity": {"var": rv, "key": ["graph", "record", "entry"],
                         "note": "one row per record (or result entry) the row variable binds, in one graph; nothing "
                                 "averaged, nothing multiplied out"},
        "columns": _column_specs(cols, contract),
        "files": {"matrix.csv": {"sha256": _sha(csv_text.encode("utf-8")), "rows": len(rows_out)}},
        "rows": manifest_rows,
        "missingness": {k: dict(sorted(v.items())) for k, v in missing.items()},
        "excluded": excluded,
        "excluded_by_query": {g["alias"]: {**((g.get("coverage") or {}).get("excluded") or {}).get("by_reason", {}),
                                           **{f"in_steps:{k}": n for k, n in ((g.get("coverage") or {}).get(
                                               "not_evaluated_in_steps") or {}).get("by_reason", {}).items()}}
                              for g in res["graphs"]},
        "identity_conflicts": res["identity_conflicts"],
        "split": {"group_by": [g["name"] for g in contract.get("groups") or []],
                  "note": "split train and test by these keys (no group on both sides) so that a specimen, a campaign or "
                          "a period is never on both; a random split of rows leaks them"},
        "transformations": [],
        "access": res["access"],
        "limitations": ["Cells are as reported: nothing converted, normalized, imputed or averaged; every absent cell "
                        "has its status. Predictions and proposals are never observations.",
                        "The table and this manifest go only where the access admits (the intersection of the graphs' "
                        "audiences)."] + ([f"Partial: {', '.join(res.get('partial_graphs') or [])} not evaluated."]
                                          if res["partial"] else []),
    }
    return csv_text, manifest


def _column_specs(cols: list[dict], contract: dict) -> list[dict]:
    out = [{"name": "row_id", "role": "identity"}, {"name": "graph", "role": "identity"},
           {"name": "record", "role": "identity"}, {"name": "entry", "role": "identity"},
           {"name": "run_status", "role": "status"}]
    for part in ("replicate", "interval"):
        if contract["row"].get(part):
            out.append({"name": part, "role": "identity", "path": contract["row"][part]})
    out += [{"name": f"group:{g['name']}", "role": "group", "path": g["path"]} for g in contract.get("groups") or []]
    for c in cols:
        spec = {k: c[k] for k in ("name", "role", "path", "type", "unit", "variable", "context", "categories", "bounds",
                                  "objective", "target", "fidelity", "select", "description") if k in c}
        spec.setdefault("type", "quantity")
        spec["columns"] = [c["name"]] + [f"{c['name']}__{s}" for s in ("status", "lower", "upper", "uncertainty",
                                                                         "uncertainty_kind")]
        out.append(spec)
    return out


def _fmt(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, (dict, list)):
        return json.dumps(x, sort_keys=True, ensure_ascii=False)
    return str(x)


def _csv(rows: list[dict], cols: list[dict], contract: dict) -> str:
    head = ["row_id", "graph", "record", "entry", "run_status"]
    head += [p for p in ("replicate", "interval") if contract["row"].get(p)]
    head += [f"group:{g['name']}" for g in contract.get("groups") or []]
    for c in cols:
        head += [c["name"]] + [f"{c['name']}__{s}" for s in ("status", "lower", "upper", "uncertainty", "uncertainty_kind")]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for r in rows:
        line = [r["row_id"], r["graph"], r["record"], r["entry"], r["run_status"]]
        line += [_fmt(r.get(p)) for p in ("replicate", "interval") if contract["row"].get(p)]
        line += [_fmt(r["groups"].get(g["name"])) for g in contract.get("groups") or []]
        for c in cols:
            cell = r["cells"][c["name"]]
            line += [_fmt(cell.get("value")), cell["status"]] + [_fmt(cell.get(k)) for k in
                                                                  ("lower", "upper", "uncertainty", "uncertainty_kind")]
        w.writerow(line)
    return buf.getvalue()


def write(out_dir: str | Path, csv_text: str, manifest: dict) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "matrix.csv").write_text(csv_text, encoding="utf-8")
    write_canonical(out / "matrix.manifest.json", manifest)
    return {"matrix": str(out / "matrix.csv"), "manifest": str(out / "matrix.manifest.json"),
            "rows": manifest["files"]["matrix.csv"]["rows"], "sha256": manifest["files"]["matrix.csv"]["sha256"],
            "access": manifest["access"]["level"]}
