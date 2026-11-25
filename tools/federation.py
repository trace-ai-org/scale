"""Queries across several scale-catal documents without merging them (docs/querying.md, "Querying across graphs").

One query plan runs over several documents, each named by an alias (``public=commons.json``,
``private=project.json``), and nothing is merged on disk or in memory:

* **Each graph on its own.** A document is read under the schema version it pins (:func:`pgraph.spec_for`), the spec
  digest of that version is computed and checked against the one this repository records for it (README.md's table)
  and, when the caller states one, against the caller's. A version the tools do not hold, a digest that differs, a
  document that cannot be read, or a plan the schema cannot express (``schema_gap``) is that graph's own status; the
  others still answer and the answer is marked ``partial``.
* **A tagged union.** Every row, record left out and missing field says which graph it comes from (``graph``,
  ``graph_sha256``) and its ``row_key`` (``<alias>/<n>``). Identity is (alias, record id): two graphs that use one id
  do not share a record by it.
* **Joins only by an identity map.** Shared reference records (species, product lumps, reactions, variables, sources,
  methods) are identified across graphs only by an evidenced identity map (:func:`load_identity_map`); the plan's
  ``join`` steps pair rows through it. One id whose definitions differ between graphs, and a mapping whose records
  disagree (a charge, a formula, a reaction's participants and coefficients), is an ``identity_conflict``: never one
  definition picked.
* **Ranking and limits after federation.** The plan's ``order_by`` and ``limit`` apply to the union; truncation is
  reported. With ``comparison: scientific`` rows of different graphs share a comparison group only when their unit,
  kind, context (a record-valued context, such as a species, matched through the identity map) and the quantity
  contract of their kind under each graph's schema agree.
* **Access is the intersection.** The answer may go only to a reader whom every consulted graph admits
  (:func:`federated_access`): the least open level, with every project and institution each graph requires; a
  membership stated in one graph never opens another's records.

A call with one graph and no alias is :func:`pgraph_query.run`, unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from . import pgraph, pgraph_query
from .passages import graph_sha256
from .util import ScaleError, dump_canonical, load_doc

ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")
NO_LIMIT = 10 ** 9
IDENTITY_MAP_VERSION = 1
# the labels of shared reference records an identity map may identify across graphs (a catalyst's formula does not
# establish a specimen's identity: samples, tests and catalysts are never mapped)
REFERENCE_LABELS = ("ChemicalSpecies", "ProductLump", "Reaction", "Variable", "Source", "Method")
# what defines a reference record: two records are one only when these agree (a field stated in one and not in the
# other disagrees for the fields in STATED_IN_BOTH: a species of unknown charge is not a species of charge 0)
DEFINING = {
    "ChemicalSpecies": ("formula", "elemental_composition", "charge", "smiles", "inchi", "species_kind", "isotopic_label"),
    "ProductLump": ("carbon_min", "carbon_max", "open_ended", "compound_class", "boiling_range", "phase"),
    "Reaction": ("direction",),
    "Variable": ("scale", "quantity_kind", "unit", "categories"),
    "Source": ("doi", "kind", "year"),
    "Method": ("method_kind", "version"),
}
STATED_IN_BOTH = {"ChemicalSpecies": ("charge", "isotopic_label")}
IDENTIFIER_KEYS = ("inchikey", "doi", "cas")
# what a record says that is not its definition (who curated it, from where, who may see it)
NOT_DEFINING = {"name", "description", "note", "evidence", "stewardship", "origin", "recorded_at", "record_version",
                "unmodeled", "identifiers", "follows_domains"}
LEAVES_GRAPH_UNREAD = ("unavailable", "unsupported_version", "spec_mismatch")


# ---------------------------------------------------------------- members

def parse_graph_args(pairs: list[str]) -> list[tuple[str, str]]:
    """``alias=path`` pairs (the CLI's repeated ``--graph``), in order; an alias is letters, digits, ``_`` and ``-``,
    starting with a letter, each once."""
    out, seen = [], set()
    for p in pairs:
        alias, sep, path = p.partition("=")
        if not sep or not path or not ALIAS.match(alias):
            raise ScaleError(f"--graph expects <alias>=<file> with an alias of letters, digits, _ or - (got {p!r})")
        if alias in seen:
            raise ScaleError(f"the alias {alias!r} names two graphs; each graph has its own alias")
        seen.add(alias)
        out.append((alias, path))
    return out


def recorded_digests(repo: Path | None) -> dict[str, str]:
    """The spec digest this repository records for each schema version (README.md's table, which `scale schema bump`
    keeps); none when the table is not there (an installed package)."""
    from .schema_change import DIGEST_TABLE
    f = Path(repo) / DIGEST_TABLE if repo else None
    if f is None or not f.is_file():
        return {}
    rows = re.findall(r"^\| scale-(?:catal|graph) (\S+)(?: \(frozen\))? \| `([0-9a-f]{64})` \|", f.read_text(encoding="utf-8"), re.M)
    return dict(rows)


def member(alias: str, *, doc: dict | None = None, spec: dict | None = None, error: str | None = None,
           status: str | None = None, path: str | None = None, expect_spec: str | None = None,
           recorded: dict[str, str] | None = None, graph: pgraph_query.Graph | None = None,
           index: Callable[[], Any] | None = None) -> dict:
    """One graph of a federated call: its alias, document and the schema it pins (or why it cannot be read: ``status``
    unavailable or unsupported_version with ``error``), the spec digest, whether it is the one this repository records
    (``spec_verified``) and the caller's (``spec_sha256`` expected: a different one is ``spec_mismatch``, never read)."""
    if not ALIAS.match(alias or ""):
        raise ScaleError(f"a graph alias is letters, digits, _ or -, starting with a letter (got {alias!r})")
    m = {"alias": alias, "doc": doc, "spec": spec, "path": path, "graph": graph, "index": index,
         "status": status, "error": error}
    if spec is not None:
        digest = pgraph.spec_sha256(spec)
        want = (recorded or {}).get(str(spec.get("version")))
        m["spec_sha256"] = digest
        m["spec_verified"] = None if want is None else want == digest
        if expect_spec and expect_spec != digest:
            m.update(status="spec_mismatch", error=f"the caller binds this graph to the spec digest {expect_spec}; "
                     f"scale-catal {spec.get('version')} here has {digest}: another schema of the same number, never "
                     "read in its place")
        elif m["spec_verified"] is False:
            m.update(status="spec_mismatch", error=f"scale-catal {spec.get('version')} here has the spec digest {digest}, "
                     f"not the {want} this repository records for it")
    if doc is not None and m["status"] is None and spec is None:
        m.update(status="unavailable", error=error or "no schema for this graph")
    return m


def load_member(alias: str, path: str | Path, spec_of: Callable[[dict], dict], *, expect_spec: str | None = None,
                recorded: dict[str, str] | None = None) -> dict:
    """A member read from ``path``: the document, and its schema by ``spec_of`` (a version the tools do not hold is
    unsupported_version, a file that cannot be read unavailable; neither stops the other graphs)."""
    try:
        doc = load_doc(path)
        if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list) or not isinstance(doc.get("links"), list):
            raise ScaleError("not a scale-catal document (node-link JSON with nodes and links)")
    except (ScaleError, OSError, ValueError) as exc:
        return member(alias, status="unavailable", error=str(exc), path=str(path))
    try:
        spec = spec_of(doc)
    except ScaleError as exc:
        return member(alias, doc=doc, status="unsupported_version", error=str(exc), path=str(path))
    return member(alias, doc=doc, spec=spec, path=str(path), expect_spec=expect_spec, recorded=recorded)


# ---------------------------------------------------------------- identity map

def load_identity_map(data: dict | str | Path | None) -> dict | None:
    """An identity map: ``{identity_map: 1, mappings: [{id, records: [{graph, record}, ...], evidence: [...]}]}``.
    Each mapping says that the records it lists, each in its graph, are one shared reference record, and why
    (``evidence``: at least one item, each an object with a ``basis``, a ``source`` and ``locator``, or a ``note``).
    Unknown keys are refused; the map is data, never instructions."""
    if data is None:
        return None
    raw = load_doc(data) if isinstance(data, (str, Path)) else data
    if not isinstance(raw, dict):
        raise ScaleError("an identity map is a JSON object {identity_map: 1, mappings: [...]}")
    extra = set(raw) - {"identity_map", "mappings", "note", "title"}
    if extra:
        raise ScaleError(f"an identity map holds identity_map, mappings, title and note, not {sorted(extra)}")
    if raw.get("identity_map") != IDENTITY_MAP_VERSION:
        raise ScaleError(f"identity_map is {IDENTITY_MAP_VERSION} (the map format's version), not {raw.get('identity_map')!r}")
    maps = raw.get("mappings")
    if not isinstance(maps, list):
        raise ScaleError("an identity map's mappings is a list")
    seen = set()
    for m in maps:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
            raise ScaleError("each mapping is an object with an id")
        if m["id"] in seen:
            raise ScaleError(f"the mapping id {m['id']!r} is used twice")
        seen.add(m["id"])
        bad = set(m) - {"id", "records", "evidence", "note"}
        if bad:
            raise ScaleError(f"mapping {m['id']}: a mapping holds id, records, evidence and note, not {sorted(bad)}")
        recs = m.get("records")
        if not isinstance(recs, list) or len(recs) < 2 or not all(
                isinstance(r, dict) and set(r) == {"graph", "record"} and all(isinstance(r[k], str) for k in r) for r in recs):
            raise ScaleError(f"mapping {m['id']}: records lists at least two {{graph, record}} objects")
    return {**raw, "sha256": hashlib.sha256(dump_canonical(raw).encode("utf-8")).hexdigest()}


def _definition(g: pgraph_query.Graph, nid: str) -> dict:
    """What defines a record for identity (:data:`DEFINING`, its identifiers), or for another label everything it
    says but who curated it and who may see it."""
    n = g.nodes[nid]
    p = n.get("props") or {}
    keys = DEFINING.get(n["label"])
    if keys is None:
        return {"label": n["label"], **{k: v for k, v in p.items() if k not in NOT_DEFINING}}
    out = {"label": n["label"], **{k: p[k] for k in keys if k in p}}
    ids = {k: v for k, v in (p.get("identifiers") or {}).items() if k in IDENTIFIER_KEYS} \
        if isinstance(p.get("identifiers"), dict) else {}
    if ids:
        out["identifiers"] = ids
    return out


def _participants(g: pgraph_query.Graph, nid: str, alias: str, class_of: Callable[[str, str], str | None]) -> list | None:
    """A reaction's participants as (role, coefficient, species identity): a species mapped by the identity map is its
    mapping, else its formula and charge; None when a participant is neither."""
    out = []
    for e in g.out.get(nid, []):
        if e["type"] != "HAS_PARTICIPANT":
            continue
        p = e.get("props") or {}
        sp = g.nodes.get(e["target"], {}).get("props") or {}
        who = class_of(alias, e["target"])
        if who is None:
            if not sp.get("formula"):
                return None
            who = f"formula {sp.get('formula')} charge {sp.get('charge')}"
        out.append([str(p.get("role")), json.dumps(p.get("coefficient")), who])
    return sorted(out)


def _differences(a: dict, b: dict, label: str) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        if k in a and k in b:
            if json.dumps(a[k], sort_keys=True) != json.dumps(b[k], sort_keys=True):
                out.append(k)
        elif k in STATED_IN_BOTH.get(label, ()) or k == "label":
            out.append(k)
        elif DEFINING.get(label) is None:            # any other label: all it says is its definition
            out.append(k)
    return out


class Identity:
    """Which records of different graphs are one shared reference record: only those an evidenced mapping of the
    identity map names, whose definitions agree. Everything else is (alias, record id)."""

    def __init__(self, members: list[dict], imap: dict | None):
        self.members = {m["alias"]: m for m in members}
        self.graphs = {a: m["graph"] for a, m in self.members.items() if m.get("graph") is not None}
        self.sha256 = (imap or {}).get("sha256")
        self.of: dict[tuple[str, str], str] = {}       # (alias, record) -> mapping id
        self.used: list[str] = []
        self.refused: list[dict] = []
        self.conflicts: list[dict] = []
        maps = (imap or {}).get("mappings") or []
        pending = []
        for m in maps:
            why = self._refusal(m)
            if why:
                self.refused.append({"mapping": m["id"], "reason": why})
            else:
                pending.append(m)
        for m in pending:                             # first the species, so that reactions compare by them
            if all(self.graphs[r["graph"]].nodes[r["record"]]["label"] != "Reaction" for r in m["records"]):
                self._admit(m)
        for m in pending:
            if any(self.graphs[r["graph"]].nodes[r["record"]]["label"] == "Reaction" for r in m["records"]):
                self._admit(m)
        self._equal_ids()

    def _refusal(self, m: dict) -> str | None:
        ev = m.get("evidence")
        if not isinstance(ev, list) or not ev or not all(
                isinstance(x, dict) and (x.get("basis") or x.get("note") or (x.get("source") and x.get("locator"))) for x in ev):
            return ("no_evidence: a mapping states why the records are one (evidence: items with a basis, a source and "
                    "locator, or a note)")
        labels = set()
        for r in m["records"]:
            if r["graph"] not in self.members:
                return f"unknown_graph: {r['graph']} is not a graph of this call"
            g = self.graphs.get(r["graph"])
            if g is None:
                return f"graph_not_read: {r['graph']} was not read ({self.members[r['graph']].get('status')})"
            n = g.nodes.get(r["record"])
            if n is None:
                return f"no_such_record: {r['record']} is not in {r['graph']}"
            labels.add(n["label"])
            if (r["graph"], r["record"]) in self.of:
                return f"mapped_twice: {r['graph']} {r['record']} is already in mapping {self.of[(r['graph'], r['record'])]}"
        if len({r["graph"] for r in m["records"]}) != len(m["records"]):
            return "one_graph_twice: a mapping names each graph once (records of one graph are not identified by a map)"
        if len(labels) != 1:
            return f"different_labels: {', '.join(sorted(labels))}"
        label = labels.pop()
        if label not in REFERENCE_LABELS:
            return (f"not_a_reference_record: {label} is not a shared reference record ({', '.join(REFERENCE_LABELS)}); "
                    "a sample, a test or a catalyst is never identified across graphs by a map")
        return None

    def _admit(self, m: dict) -> None:
        defs = []
        for r in m["records"]:
            g = self.graphs[r["graph"]]
            d = _definition(g, r["record"])
            if d["label"] == "Reaction":
                d["participants"] = _participants(g, r["record"], r["graph"], self.class_of)
                if d["participants"] is None:
                    d["participants"] = "a participant neither mapped nor with a formula"
            defs.append((r, d))
        first_r, first = defs[0]
        diffs = sorted({k for _, d in defs[1:] for k in _differences(first, d, first["label"])})
        if any(isinstance(d.get("participants"), str) for _, d in defs):
            diffs = sorted(set(diffs) | {"participants"})
        if diffs:
            self.conflicts.append({"kind": "mapping", "mapping": m["id"], "label": first["label"],
                                   "records": [dict(r) for r, _ in defs], "differ_in": diffs,
                                   "note": "the mapping names records whose definitions disagree; it is not used, and "
                                           "neither definition is taken for the other"})
            self.refused.append({"mapping": m["id"], "reason": f"identity_conflict: {', '.join(diffs)}"})
            return
        for r in m["records"]:
            self.of[(r["graph"], r["record"])] = m["id"]
        self.used.append(m["id"])

    def _equal_ids(self) -> None:
        """One id in several graphs: a conflict when what they say differs, never one of them picked; the same id
        with the same definition is still two records unless a mapping identifies them."""
        aliases = list(self.graphs)
        self.shared_ids = 0
        for i, a in enumerate(aliases):
            for b in aliases[i + 1:]:
                ga, gb = self.graphs[a], self.graphs[b]
                for nid in sorted(set(ga.nodes) & set(gb.nodes)):
                    self.shared_ids += 1
                    da, db = _definition(ga, nid), _definition(gb, nid)
                    diffs = _differences(da, db, da["label"]) if da["label"] == db["label"] else ["label"]
                    if diffs:
                        self.conflicts.append({"kind": "equal_id", "record": nid, "graphs": [a, b],
                                               "labels": sorted({da["label"], db["label"]}), "differ_in": diffs,
                                               "note": "one id, two definitions: each graph's record is its own "
                                                       "(alias, id), never one of them taken for both"})

    def class_of(self, alias: str, rid: str) -> str | None:
        return self.of.get((alias, rid))

    def conflicted(self) -> set[tuple[str, str]]:
        out = set()
        for c in self.conflicts:
            if c["kind"] == "equal_id":
                out |= {(g, c["record"]) for g in c["graphs"]}
            else:
                out |= {(r["graph"], r["record"]) for r in c["records"]}
        return out

    def report(self) -> dict:
        return {"map_sha256": self.sha256, "mappings_used": self.used, "refused": self.refused,
                "ids_in_several_graphs": getattr(self, "shared_ids", 0)}


# ---------------------------------------------------------------- access

def graph_audiences(doc: dict, as_of: str) -> list[dict]:
    """Who may see every record of ``doc``, as the readers its least open records require: each distinct
    non-public (level, project or institutions) of its header and records (:func:`pgraph.access_key`)."""
    graph = doc.get("graph") if isinstance(doc.get("graph"), dict) else {}
    sts = [pgraph.stewardship_of(graph.get("stewardship"))]
    sts += [pgraph.effective_stewardship(doc, r) for r in list(doc.get("nodes") or []) + list(doc.get("links") or [])
            if isinstance(r, dict)]
    keys = {pgraph.access_key(st, as_of) for st in sts}
    out = []
    for level, scope in sorted(keys, key=lambda k: (pgraph.OPENNESS.index(k[0]), json.dumps(k[1]))):
        if level == "public":
            continue
        d = {"level": level}
        if level == "project":
            d["project"] = scope or pgraph.SOME_PROJECT
        elif level == "institution":
            d["institutions"] = list(scope or ())
        out.append(d)
    return out


def federated_access(members: list[dict], as_of: str) -> dict:
    """The access of an answer drawn from several graphs: the intersection of their audiences, never the most open.
    ``level`` is the least open of every consulted graph's whole-document access (:func:`pgraph_query.document_access`,
    a graph that could not be read counting as institution: it fails closed), ``requires`` every audience a graph's
    records require, and ``disjoint`` says when no reader is named that all of them admit (two projects, institutions
    with none in common)."""
    levels, requires, per = [], [], {}
    for m in members:
        doc = m.get("doc")
        if doc is None:
            acc = {"level": "institution", "note": "the graph could not be read; its access is unknown and read as institution"}
            auds = [{"level": "institution", "institutions": []}]
        else:
            acc = pgraph_query.document_access(doc, as_of)
            auds = graph_audiences(doc, as_of)
        per[m["alias"]] = acc
        levels.append(acc["level"])
        requires += [{"graph": m["alias"], **a} for a in auds]
    level = max(levels, key=pgraph.OPENNESS.index) if levels else "institution"
    projects = sorted({r["project"] for r in requires if r["level"] == "project"})
    insts = [set(r["institutions"]) for r in requires if r["level"] == "institution"]
    disjoint = len(projects) > 1 or (bool(insts) and (not all(insts) or not set.intersection(*insts))) \
        or (bool(insts) and bool(projects))
    if level == "public":
        note = (f"Every record of the {len(members)} queried graphs, and their headers, is public: the answer carries no "
                "access limit of its own.")
    else:
        closed = sorted({r["graph"] for r in requires})
        note = (f"The answer rests on {len(members)} graphs, {', '.join(closed)} not all public: it may go only to a reader "
                "whom every one of them admits (the intersection of their audiences, `requires`), whatever part of it a row "
                "shows; a membership one graph states never opens another graph's records.")
        if disjoint:
            note += (" The audiences required are disjoint as far as the graphs name them (" +
                     "; ".join(([f"projects {', '.join(projects)}"] if len(projects) > 1 else [])
                               + (["institutions with none in common"] if insts else [])) +
                     "): only a reader admitted by each, if any, may see the answer; it is never relabelled to the more "
                     "open audience. For a wider answer, query the views `scale graph filter` makes for that audience.")
    return {"level": level, "basis": "intersection", "requires": requires, "disjoint": disjoint,
            "graphs": per, "note": note}


# ---------------------------------------------------------------- query

def _plan_for_graph(plan: dict, extra_returns: list[str]) -> dict:
    p = copy.deepcopy(plan)
    p.pop("join", None)
    p["limit"] = NO_LIMIT
    if extra_returns:
        p["return"] = list(p.get("return") or []) + extra_returns
    return p


def _order_paths(plan: dict) -> list[str]:
    root = (plan.get("match") or {}).get("as", "self")
    return [f"{ob.get('var', root)}.{ob['prop']}" for ob in plan.get("order_by") or [] if isinstance(ob, dict) and ob.get("prop")]


def _sort_key(v: Any, scientific: bool) -> tuple:
    if scientific and isinstance(v, list) and len(v) == 1:
        v = v[0]
    lo, hi = pgraph_query.number(v)
    x = lo if lo is not None else hi
    return (x is None, x if x is not None else 0.0)


def _contract_fp(spec: dict, kind: str | None) -> str | None:
    """The quantity contract of ``kind`` under one graph's schema (its kind and the contexts it is compared by), as a
    digest: rows of two graphs are compared together only when their contracts agree."""
    q = (spec or {}).get("quantities") or {}
    k = (q.get("kinds") or {}).get(kind) if kind else None
    if k is None:
        return None
    ctx = {c: (q.get("contexts") or {}).get(c) for c in k.get("compare_by") or []}
    return hashlib.sha256(json.dumps({"kind": k, "contexts": ctx}, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _check_joins(plan: dict, aliases: list[str]) -> list[dict]:
    joins = plan.get("join") or []
    if not isinstance(joins, list):
        raise ScaleError("join is a list of {var, graphs} steps")
    root = (plan.get("match") or {}).get("as", "self")
    node_vars = {root} | {st.get("as") for st in plan.get("steps") or [] if "edge" in st or "field" in st}
    for j in joins:
        if not isinstance(j, dict) or set(j) - {"var", "graphs", "as"} or not j.get("var"):
            raise ScaleError("a join step is {var, graphs, as}: the variable whose records are joined across graphs, the "
                             "graphs (default: all) and a name")
        if j["var"] not in node_vars:
            raise ScaleError(f"join var {j['var']!r} is not a record variable of the plan (match or an edge or field step)")
        gs = j.get("graphs") or aliases
        unknown = [g for g in gs if g not in aliases]
        if unknown or len(set(gs)) < 2:
            raise ScaleError(f"a join names at least two graphs of the call ({', '.join(aliases)}); got {gs}")
    return joins


def run(members: list[dict], plan: dict, identity_map: dict | None = None, limit: int | None = None) -> dict:
    """Run ``plan`` on each graph of ``members`` (:func:`member`, in the order given) and federate the answers: a tagged
    union of rows, the joins the plan asks for through ``identity_map`` (:func:`load_identity_map`), ranking and limit
    after federation, per-graph coverage, versions and digests, and the intersection of the graphs' audiences.
    ``limit`` overrides the plan's (the design matrix reads every row)."""
    if not members:
        raise ScaleError("a federated query names at least one graph")
    aliases = [m["alias"] for m in members]
    if len(set(aliases)) != len(aliases):
        raise ScaleError("each graph of a federated query has its own alias")
    plan = json.loads(json.dumps(plan, default=pgraph_query._plain))
    joins = _check_joins(plan, aliases)
    scientific = plan.get("comparison") == "scientific"
    as_of = pgraph_query.embargo_day(plan.get("as_of"), pgraph.embargo_today())
    order = _order_paths(plan)
    hidden = [p for p in order if p not in (plan.get("return") or [])]
    per_plan = _plan_for_graph(plan, hidden)

    for m in members:
        if m["status"] is None and m.get("graph") is None:
            m["graph"] = pgraph_query.Graph(m["doc"], m["spec"])
    identity = Identity(members, identity_map)
    conflicted = identity.conflicted()

    graphs, rows, excluded, missing = [], [], [], []
    for m in members:
        info = {"alias": m["alias"], **({"path": m["path"]} if m.get("path") else {}),
                "graph_id": ((m.get("doc") or {}).get("graph") or {}).get("graph_id"),
                "schema_version": ((m.get("doc") or {}).get("graph") or {}).get("schema_version"),
                "spec_sha256": m.get("spec_sha256"), "spec_verified": m.get("spec_verified")}
        if m["status"] in LEAVES_GRAPH_UNREAD:
            info.update(status=m["status"], error=m["error"], evaluated=False,
                        graph_sha256=m["graph"].digest if m.get("graph") is not None else None)
            graphs.append(info)
            continue
        try:
            ix = m["index"]() if (plan.get("text") and m.get("index")) else None
            res = pgraph_query.run(m["doc"], m["spec"], per_plan, ix, m["graph"])
        except ScaleError as exc:
            info.update(status="plan_error", error=str(exc), evaluated=False, graph_sha256=m["graph"].digest)
            graphs.append(info)
            continue
        info.update(status=res["status"], evaluated=res["status"] != "schema_gap", graph_sha256=res["graph_sha256"],
                    row_count=res.get("row_count", 0), coverage=res.get("coverage"),
                    excluded_total=(res.get("coverage") or {}).get("excluded", {}).get("total", 0),
                    limitations=res.get("limitations"))
        if res.get("schema_gaps"):
            info["schema_gaps"] = res["schema_gaps"]
        if res.get("comparison"):
            info["comparison"] = res["comparison"]
        graphs.append(info)
        for i, r in enumerate(res.get("rows") or []):
            r = dict(r)
            r["graph"], r["graph_sha256"], r["row_key"] = m["alias"], res["graph_sha256"], f"{m['alias']}/{i}"
            ids = {v if isinstance(v, str) else (v or {}).get("record") for v in r["bound"].values()}
            hit = sorted(x for x in ids if x and (m["alias"], x) in conflicted)
            if hit:
                r["identity_conflict"] = hit
            r["_keys"] = [_sort_key(r["values"].get(p), scientific) for p in order]
            r["_m"] = m
            for p in hidden:
                r["values"] = {k: v for k, v in r["values"].items() if k != p}
            rows.append(r)
        excluded += [{"graph": m["alias"], **x} for x in res.get("excluded") or []]
        missing += [{"graph": m["alias"], **x} for x in res.get("missing_context") or []]

    # global ranking: each order_by from the last, stable (graph order, then each graph's own order)
    obs = plan.get("order_by") or []
    for i in reversed(range(len(obs))):
        rows.sort(key=lambda r, i=i: r["_keys"][i], reverse=bool(obs[i].get("desc")))
    groups: list[dict] = []
    if scientific:
        keyed: dict[str, int] = {}
        for r in rows:
            m = r["_m"]
            on = {}
            for path, t in sorted((r.get("compared") or {}).items()):
                ctx = {}
                for k, v in sorted((t.get("context") or {}).items()):
                    if isinstance(v, str) and v in m["graph"].nodes:
                        cls = identity.class_of(m["alias"], v)
                        v = {"identity": cls} if cls else {"graph": m["alias"], "record": v}
                    ctx[k] = v
                on[path] = {"unit": t.get("unit"), **({"kind": t["kind"]} if t.get("kind") else {}),
                            **({"context": ctx} if ctx else {}),
                            **({"contract": _contract_fp(m["spec"], t.get("kind"))} if t.get("kind") else {})}
            k = json.dumps(on, sort_keys=True, ensure_ascii=False)
            if k not in keyed:
                keyed[k] = len(groups)
                groups.append({"group": keyed[k], "compared_on": on, "rows": 0, "graphs": []})
            g = groups[keyed[k]]
            g["rows"] += 1
            if m["alias"] not in g["graphs"]:
                g["graphs"].append(m["alias"])
            r["graph_comparison_group"] = r.get("comparison_group")
            r["comparison_group"] = keyed[k]
        rows.sort(key=lambda r: r["comparison_group"])            # stable: each group keeps its ranking
    if graphs and all(g["status"] == "plan_error" for g in graphs):
        raise ScaleError(graphs[0]["error"])              # the plan itself is wrong, as for one graph
    joined = _joins(joins, rows, identity, aliases)
    for r in rows:
        r.pop("_keys", None)
        r.pop("_m", None)
    total = len(rows)
    cap = limit if limit is not None else plan.get("limit", 200)
    page = rows[:cap]

    evaluated = [g for g in graphs if g.get("evaluated")]
    partial = [g["alias"] for g in graphs if not g.get("evaluated")]
    if page:
        status = "answered"
    elif any(g["status"] == "missing_context" for g in evaluated):
        status = "missing_context"
    elif evaluated:
        status = "no_matching_record"
    elif any(g["status"] == "schema_gap" for g in graphs):
        status = "schema_gap"
    else:
        status = "unavailable"
    access = federated_access(members, as_of)
    lim = ["Each graph was queried on its own, under the schema version it pins; nothing was merged. A row is one graph's "
           "(its graph and row_key say which), and a record is identified by its graph and id."]
    if partial:
        lim.append(f"Partial answer: {', '.join(partial)} " + ("was" if len(partial) == 1 else "were") + " not evaluated ("
                   + "; ".join(f"{g['alias']} {g['status']}" for g in graphs if not g.get("evaluated"))
                   + "): an absence there is unknown, not no_matching_record.")
    if total > len(page):
        lim.append(f"The limit {cap} applied after federation: {len(page)} of {total} rows are returned (truncated).")
    if obs:
        lim.append("order_by ranked the rows of every graph together, by the numbers as reported"
                   + (", within each comparison group only" if scientific else "") + ".")
        if not scientific:
            units = {}
            for p in order:
                for r in rows:
                    v = r["values"].get(p) if p in r["values"] else None
                    if isinstance(v, dict) and isinstance(v.get("unit"), str):
                        units.setdefault(p, set()).add(v["unit"])
            for p, us in units.items():
                if len(us) > 1:
                    lim.append(f"order_by {p} sorted values in several units as bare numbers ({', '.join(sorted(us))}); "
                               "read each with its unit.")
    if scientific and groups:
        mixed = [g for g in groups if len(g["graphs"]) > 1]
        lim.append(f"The rows fall into {len(groups)} comparison groups across the graphs; {len(mixed)} hold rows of "
                   "several graphs, which share a unit, a kind, a context (a record-valued context, such as a species, "
                   "only through the identity map) and the quantity contract of that kind under each graph's schema. "
                   "Nothing is compared, or ranked, across groups.")
    if identity.conflicts:
        lim.append(f"{len(identity.conflicts)} identity conflicts (identity_conflicts): one id, or one mapping, with two "
                   "definitions; neither is taken for the other, and no join goes through them.")
    if joins and not identity.used:
        lim.append("The plan joins rows across graphs, but the identity map identifies no record (no map, or every "
                   "mapping refused): equal ids are never joined by themselves.")
    if access["level"] != "public":
        lim.append(f"Access {access['level']} (the intersection of the graphs' audiences): {access['note']}")
    return {"federated": True, "question": plan.get("question"), "plan": plan, "status": status,
            "partial": bool(partial), **({"partial_graphs": partial} if partial else {}),
            "graphs": graphs, "row_count": total, "truncated": total > len(page), "rows": page,
            "excluded": excluded, "missing_context": missing, "identity": identity.report(),
            "identity_conflicts": identity.conflicts, **({"joins": joined} if joins else {}),
            **({"comparison": {"mode": "scientific", "groups": groups}} if scientific else {}),
            "coverage": {"graphs": {g["alias"]: g.get("coverage") for g in graphs},
                         "limit": {"rows_in": total, "rows_out": len(page)}, "truncated": total > len(page)},
            "access": access, "limitations": lim}


def _joins(joins: list[dict], rows: list[dict], identity: Identity, aliases: list[str]) -> list[dict]:
    """Each join step's matches: per mapping of the identity map, the rows of each graph whose variable binds one of
    its records (grouped, never a cartesian product of rows); equal ids of unmapped records are counted, not joined."""
    out = []
    for j in joins:
        gs = j.get("graphs") or aliases
        by_class: dict[str, dict[str, list]] = {}
        unmapped: dict[str, set] = {}
        for r in rows:
            if r["graph"] not in gs:
                continue
            rid = r["bound"].get(j["var"])
            if not isinstance(rid, str):
                continue
            cls = identity.class_of(r["graph"], rid)
            if cls is None:
                unmapped.setdefault(rid, set()).add(r["graph"])
                continue
            by_class.setdefault(cls, {}).setdefault(r["graph"], []).append({"row_key": r["row_key"], "record": rid})
        matches = [{"identity": cls, "graphs": {g: by_class[cls][g] for g in gs if g in by_class[cls]}}
                   for cls in sorted(by_class) if len(by_class[cls]) > 1]
        out.append({"var": j["var"], **({"as": j["as"]} if j.get("as") else {}), "graphs": list(gs), "matches": matches,
                    "equal_ids_not_mapped": sorted(rid for rid, g in unmapped.items() if len(g) > 1)})
    return out


# ---------------------------------------------------------------- search

def search(members: list[dict], query: str, k: int = 10, mode: str = "hybrid", published_before: str | None = None) -> dict:
    """Search the evidence of each graph (its own index, ``member['index']()``) and federate the hits: each hit says
    its graph; the hits are interleaved by their rank in their own graph (scores of separate indexes are never
    compared), ``k`` applies after federation and truncation is reported; the access is the intersection of the
    graphs' audiences."""
    from .passages import search_quotes
    before = int(str(published_before)[:4]) if published_before else None
    graphs, hits = [], []
    for order, m in enumerate(members):
        if m["status"] in LEAVES_GRAPH_UNREAD and m.get("doc") is None:
            graphs.append({"alias": m["alias"], "status": m["status"], "error": m["error"], "evaluated": False})
            continue
        doc = m["doc"]
        if m.get("index") is not None:
            ix = m["index"]()
        else:                                        # the evidence quotes do not depend on the schema
            from .passages import Index, evidence_items
            ix = Index.build(evidence_items(doc), graph_sha256=graph_sha256(doc))
        nodes = {n["id"]: n for n in doc["nodes"]}
        links = {e["id"]: e for e in doc["links"]}
        years = {n["id"]: (n.get("props") or {}).get("year") for n in doc["nodes"] if n.get("label") == "Source"}
        where = (lambda it, years=years: years.get(it.get("source")) is not None and years[it["source"]] < before) \
            if before else None

        def name_of(rid, nodes=nodes, links=links):
            rec = nodes.get(rid) or links.get(rid)
            return (rec or {}).get("props", {}).get("name") or (rec or {}).get("type")
        found = search_quotes(ix, query, k=k, mode=mode, where=where, name_of=name_of)
        digest = graph_sha256(doc)
        for rank, h in enumerate(found, 1):
            hits.append({**h, "source_year": years.get(h.get("source")), "graph": m["alias"], "graph_sha256": digest,
                         "rank_in_graph": rank, "_order": order})
        graphs.append({"alias": m["alias"], "status": "searched", "evaluated": True, "graph_sha256": digest,
                       "embedder": ix.embedder_id, "hits": len(found)})
    hits.sort(key=lambda h: (h["rank_in_graph"], h["_order"]))
    for h in hits:
        h.pop("_order", None)
    total = len(hits)
    access = federated_access(members, pgraph.embargo_today())
    return {"federated": True, "query": query, "mode": mode, "graphs": graphs, "hits": hits[:k], "hit_count": total,
            "truncated": total > k, "ranking": "by rank within each graph, interleaved (the scores of separate indexes "
                                               "are never compared)",
            "partial": any(not g["evaluated"] for g in graphs), "access": access}


# ---------------------------------------------------------------- rendering

def render_markdown(res: dict) -> str:
    acc = res.get("access") or {}
    lines = [f"# {res.get('question') or 'Query'}", "",
             f"status: **{res['status']}**{' (partial)' if res.get('partial') else ''} · rows: {res.get('row_count', 0)}"
             f"{' (truncated)' if res.get('truncated') else ''} · access: **{acc.get('level')}** (intersection)", "",
             "| graph | status | schema | spec digest | verified | graph digest | rows |", "|---|---|---|---|---|---|---|"]
    for g in res["graphs"]:
        lines.append(f"| {g['alias']} | {g['status']} | {g.get('schema_version')} | {(g.get('spec_sha256') or '')[:12]} | "
                     f"{g.get('spec_verified')} | {(g.get('graph_sha256') or '')[:12]} | {g.get('row_count', '')} |")
    lines.append("")
    if acc.get("level") != "public":
        lines += [f"Access: {acc.get('note')}", ""]
    for i, r in enumerate(res["rows"], 1):
        lines.append(f"## {i}. [{r['graph']}] {r['bound']}")
        for k, v in r["values"].items():
            lines.append(f"- {k}: {pgraph_query._render_value(v)}")
        if r.get("identity_conflict"):
            lines.append(f"- **identity conflict: {', '.join(r['identity_conflict'])}**")
        for ev in (r.get("evidence") or [])[:6]:
            q = f" — “{ev['quote']}”" if ev.get("quote") else ""
            lines.append(f"  - evidence ({ev.get('source_year')}) {ev.get('source')}, {ev.get('locator')}{q}")
        lines.append("")
    for j in res.get("joins") or []:
        lines.append(f"Join on {j['var']}: {len(j['matches'])} shared records")
        for mt in j["matches"]:
            lines.append(f"- {mt['identity']}: " + "; ".join(f"{g} {', '.join(x['row_key'] for x in xs)}"
                                                           for g, xs in mt["graphs"].items()))
    if res.get("identity_conflicts"):
        lines += ["", "Identity conflicts:"] + [f"- {c.get('mapping') or c.get('record')}: {', '.join(c['differ_in'])}"
                                                for c in res["identity_conflicts"]]
    lines += ["", "Limitations:"] + [f"- {x}" for x in res["limitations"]]
    return "\n".join(lines) + "\n"
