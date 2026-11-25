"""The model view of a query or search result: the same answer without its repetition, for a model to read.

A query result repeats itself: every row carries each evidence item whole (its quote, locator and its source's year, kind,
publication status and peer review), the same passage once per record and link that cites it, and every id spells the
graph's prefix. ``pack_query`` and ``pack_search`` write a result once more, removing repetition only:

* each distinct evidence item (its source, locator, kind, quote or transcription, passage, …) once, in ``evidence``, as
  ``E1`` … ``En``, numbered over the whole result so that an id means the same item on every page; a text that two items
  share is written once (``quote_of`` / ``transcription_of``: the text of that item);
* each row's evidence as its ownership: per record of the row, ``{"var", "items": "0:E1 3:E4"}`` (item 0 of that record's
  evidence is E1, item 3 is E4), with ``record`` and ``entry`` stated when they are not the row's binding of ``var``
  (a link, or a result entry another variable owns); the order of the full result is kept;
* each source once, in ``sources``: the fields its evidence items all repeat (``year``, ``status``, ``kind``,
  ``peer_reviewed``), and to name it its ``title`` (shortened), ``doi`` and ``name`` (read from the document; not in the
  full result);
* the values of a row as a list in the order of ``columns``;
* each filter (or ranking) that records were left out by once, in ``filters``, an excluded record naming it
  (``filter_ref``, ``order_by_ref``);
* every id that starts with ``id_prefix`` written ``~`` and the rest (no string of the result starts with ``~``, or no
  prefix is used).

Nothing else is changed: values (as reported, unit, basis, bounds, status, note), bound variables and record ids,
coverage, exclusions, missing context, limitations, access and the graph's identity are the full result's.
:func:`expand` restores the full result (with a page's rows); ``tools/tests/test_modelview.py`` proves the round trip on
real plans. A query's rows come in pages (``page``: offset, rows, of and ``next``, the arguments that fetch the next
page): no row is ever dropped without saying so, and ``counts`` states rows matched, rows the plan's limit returned and
the distinct records per variable.
"""

from __future__ import annotations

import json
from collections import Counter

from .util import ScaleError

NAME = "model"
VERSION = 1
PAGE_SIZE = 100                                            # rows of a page at most
PAGE_CHARS = 40000                                         # and about this many characters of rows and their evidence
MAX_PAGE_SIZE = 500
_SOURCE_FIELDS = {"source_year": "year", "source_status": "status", "source_kind": "kind",
                  "source_peer_reviewed": "peer_reviewed"}
_OCCURRENCE = ("var", "record", "entry", "evidence")       # what places an evidence item in a row, not the item
_TEXTS = ("quote", "transcription")
_VIEW_KEYS = ("view", "id_prefix", "columns", "evidence", "sources", "page", "counts", "filters")
_FILTER_KEYS = ("filter", "order_by")                     # an excluded record's filter or ranking, as the plan wrote it
_MIN_SHARED_TEXT = 16                                      # a shorter text is cheaper written again than referred to

QUERY_LEGEND = (
    "Same answer as the full result, each repetition written once. An id starting with ~ is id_prefix followed by the "
    "rest. rows[].values follow columns. rows[].evidence says whose evidence each item is: for each record of the row, "
    "var (the plan variable, or a link type) and items 'i:En' (item i of that record's evidence is evidence En); record "
    "(and entry) are given only when they are not the row's binding of var. evidence.En: the item once (source, "
    "locator, kind, quote or transcription; quote_of En: the same text as En). sources: each source once, with the "
    "year, publication status, kind and peer review every item of it carries. excluded[].filter_ref (order_by_ref): "
    "the plan's filter in filters. page.next: the arguments that fetch the next rows; counts.rows_matched counts every "
    "match, rows_returned what the plan's limit kept.")
SEARCH_LEGEND = (
    "Same hits as the full result, each repetition written once. An id starting with ~ is id_prefix followed by the "
    "rest. A hit's id is its record#evidence/i unless given; sources: each source once, with the year its hits carry; "
    "an also entry without record_name is named by its label.")


# ------------------------------------------------------------------------------------------------- ids


def _strings(x, out: set) -> None:
    if isinstance(x, str):
        out.add(x)
    elif isinstance(x, dict):
        for k, v in x.items():
            out.add(k)
            _strings(v, out)
    elif isinstance(x, list):
        for v in x:
            _strings(v, out)


def choose_prefix(full: dict, graph_id: str | None = None) -> str | None:
    """The id prefix the view writes as ``~``: the graph id's namespace up to its first ``/`` (``ex:msyn/``), else the
    namespace most ids share; none when any string of the result starts with ``~`` (the view would not be reversible)
    or fewer than five strings start with it."""
    seen: set = set()
    _strings(full, seen)
    if any(s.startswith("~") for s in seen):
        return None
    cands = Counter()
    for s in seen:
        head, slash, _ = s.partition("/")
        if slash and ":" in head and " " not in head:
            cands[head + "/"] += 1
    pick = None
    if graph_id and "/" in graph_id and ":" in graph_id.split("/", 1)[0]:
        pick = graph_id.split("/", 1)[0] + "/"
    elif cands:
        pick = cands.most_common(1)[0][0]
    n = sum(1 for s in seen if pick and s.startswith(pick))
    return pick if pick and n >= 5 else None


def _map_strings(x, f):
    if isinstance(x, str):
        return f(x)
    if isinstance(x, dict):
        return {f(k): _map_strings(v, f) for k, v in x.items()}
    if isinstance(x, list):
        return [_map_strings(v, f) for v in x]
    return x


def _shorten(x, prefix: str | None):
    if not prefix:
        return x
    n = len(prefix)
    return _map_strings(x, lambda s: "~" + s[n:] if s.startswith(prefix) else s)


def _lengthen(x, prefix: str | None):
    if not prefix:
        return x
    return _map_strings(x, lambda s: prefix + s[1:] if s.startswith("~") else s)


# ------------------------------------------------------------------------------------------------- sources


def _short_title(t) -> str | None:
    if not isinstance(t, str):
        return None
    t = " ".join(t.split())
    if len(t) <= 90:
        return t
    cut = t[:90].rsplit(" ", 1)[0]
    return cut + "…"


def _source_names(doc: dict | None, ids: set) -> dict:
    """What names a source (its shortened title, name if it differs, DOI) as the document records it."""
    if not doc:
        return {}
    out = {}
    for n in doc.get("nodes") or []:
        if n.get("id") in ids:
            p = n.get("props") or {}
            title = _short_title(p.get("title") or p.get("name"))
            name = p.get("name") if p.get("name") and p.get("name") != p.get("title") else None
            out[n["id"]] = {**({"title": title} if title else {}), **({"name": name} if name else {}),
                            **({"doi": p["doi"]} if p.get("doi") else {})}
    return out


def _constant_source_fields(items: list[dict], fields) -> dict:
    """Per source: the fields (of ``fields``) every one of its items carries with one value."""
    vals: dict = {}
    for it in items:
        s = it.get("source")
        if not isinstance(s, str):
            continue
        per = vals.setdefault(s, {f: set() for f in fields})
        for f in fields:
            per[f].add(json.dumps(it[f], sort_keys=True) if f in it else "\0missing")
    return {s: {f: json.loads(next(iter(v))) for f, v in per.items() if len(v) == 1 and "\0missing" not in v}
            for s, per in vals.items()}


# ------------------------------------------------------------------------------------------------- query


def _derived(row: dict, var: str) -> tuple:
    """The record (and entry) whose evidence ``var`` names in a row, when the row's binding says it."""
    b = (row.get("bound") or {}).get(var)
    if isinstance(b, str):
        return b, None
    if isinstance(b, dict) and "record" in b:
        return b["record"], b.get("path")
    if var not in (row.get("bound") or {}) and len(row.get("links") or []) == 1:
        return row["links"][0], None
    return None, None


def _check_keys(full: dict) -> None:
    clash = [k for k in _VIEW_KEYS if k in full]
    if clash:
        raise ScaleError(f"the result already has {clash}: no model view of it (internal)")


def _chars(x) -> int:
    return len(json.dumps(x, separators=(",", ":"), ensure_ascii=False))


def _whole(name: str, x, lo: int, hi: int) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or not lo <= x <= hi:
        raise ScaleError(f"{name} is a whole number from {lo} to {hi}, not {x!r}")
    return x


def pack_query(full: dict, offset: int = 0, page_size: int | None = None, doc: dict | None = None,
               page_chars: int | None = None) -> dict:
    """The model view of a query result (``scale_query``'s ``view: model``): a page of its rows from ``offset``, at
    most ``page_size`` rows and, past the first row, at most about ``page_chars`` characters of rows and the evidence
    items they bring, with every other part of the result whole. ``doc``: the document queried (names the sources)."""
    if "error" in full:
        return full
    _check_keys(full)
    offset = _whole("offset", offset, 0, 10**9)
    page_size = _whole("page_size", PAGE_SIZE if page_size is None else page_size, 1, MAX_PAGE_SIZE)
    page_chars = _whole("page_chars", PAGE_CHARS if page_chars is None else page_chars, 1000, 10**8)
    rows = full.get("rows") or []
    items = [it for r in rows for it in (r.get("evidence") or [])]
    moved = _constant_source_fields(items, list(_SOURCE_FIELDS))

    def content(it: dict) -> dict:
        m = moved.get(it.get("source")) or {}
        return {k: v for k, v in it.items() if k not in _OCCURRENCE and k not in m}

    eid: dict[str, str] = {}
    table: dict[str, dict] = {}
    for it in items:                                    # numbered over the whole result: E7 is E7 on every page
        c = content(it)
        key = json.dumps(c, sort_keys=True, ensure_ascii=False)
        if key not in eid:
            eid[key] = f"E{len(eid) + 1}"
            table[eid[key]] = c

    columns = list(rows[0].get("values") or {}) if rows else list((full.get("plan") or {}).get("return") or [])

    def pack_row(r: dict) -> tuple[dict, list[str]]:
        o, mine = {}, []
        for k, v in r.items():
            if k == "values" and isinstance(v, dict) and list(v) == columns:
                o[k] = [v[c] for c in columns]
            elif k == "evidence" and isinstance(v, list):
                groups: list[dict] = []
                last = None
                for it in v:
                    e = eid[json.dumps(content(it), sort_keys=True, ensure_ascii=False)]
                    mine.append(e)
                    who = (it.get("var"), it.get("record"), it.get("entry"))
                    if who != last:
                        rec, ent = _derived(r, it.get("var"))
                        g = {"var": it.get("var")}
                        if rec != it.get("record"):
                            g["record"] = it.get("record")
                        if ent != it.get("entry"):
                            g["entry"] = it.get("entry")
                        g["items"] = []
                        groups.append(g)
                        last = who
                    groups[-1]["items"].append(f"{it.get('evidence')}:{e}")
                for g in groups:
                    g["items"] = " ".join(g["items"])
                o[k] = groups
            else:
                o[k] = v
        return o, mine

    # a page: rows from offset, at most page_size of them and, past the first, within page_chars (the rows and the
    # evidence items they bring); the rest is reached by page.next, never dropped
    used: set[str] = set()
    out_rows: list[dict] = []
    size = 0
    for r in rows[offset:]:
        if len(out_rows) >= page_size:
            break
        o, mine = pack_row(r)
        new = [e for e in dict.fromkeys(mine) if e not in used]
        cost = _chars(o) + sum(_chars(table[e]) + len(e) + 4 for e in new)
        if out_rows and size + cost > page_chars:
            break
        size += cost
        out_rows.append(o)
        used.update(new)
    page = out_rows

    ev: dict[str, dict] = {}
    first_with: dict[str, str] = {}
    for e in sorted(used, key=lambda x: int(x[1:])):
        c = dict(table[e])
        for f in _TEXTS:
            t = c.get(f)
            if isinstance(t, str) and len(t) >= _MIN_SHARED_TEXT:
                if t in first_with:
                    del c[f]
                    c[f + "_of"] = first_with[t]
                else:
                    first_with[t] = e
        ev[e] = c
    srcs = sorted({c["source"] for c in ev.values() if isinstance(c.get("source"), str)})
    names = _source_names(doc, set(srcs))
    sources = {s: {**{_SOURCE_FIELDS[f]: v for f, v in (moved.get(s) or {}).items()}, **names.get(s, {})} for s in srcs}

    distinct: dict[str, set] = {}
    for r in rows:
        for var, b in (r.get("bound") or {}).items():
            if b is not None:
                distinct.setdefault(var, set()).add(json.dumps(b, sort_keys=True))
    cited = {it.get("source") for it in items if it.get("source")}
    row_count = full.get("row_count", len(rows))
    end = offset + len(page)
    counts = {"rows_matched": row_count, "rows_returned": len(rows),
              **({"not_returned_by_limit": row_count - len(rows),
                  "how_to_reach_them": f"the plan's limit (default 200) kept {len(rows)} of {row_count} matches: run "
                                       f"the plan again with a larger limit (e.g. {row_count}) and page through"}
                 if isinstance(row_count, int) and row_count > len(rows) else {}),
              "distinct_records": {k: len(v) for k, v in distinct.items()},
              **({"sources_cited": len(cited)} if items else {})}
    pg = {"offset": offset, "rows": len(page), "of": len(rows),
          "next": {"offset": end, "page_size": page_size, "page_chars": page_chars,
                   "how": "call scale_query again with the same plan and path, view model and these arguments"}
          if end < len(rows) else None}
    if offset and offset >= len(rows):
        pg["note"] = f"offset {offset} is past the {len(rows)} rows returned"

    filters: dict[str, dict] = {}            # the plan's filters (and rankings) the records left out name, each once
    fid: dict[str, str] = {}
    excluded = []
    for x in full.get("excluded") or []:
        o = {}
        for k, v in x.items():
            if k in _FILTER_KEYS and isinstance(v, dict):
                key = json.dumps(v, sort_keys=True, ensure_ascii=False)
                if key not in fid:
                    fid[key] = f"F{len(fid) + 1}"
                    filters[fid[key]] = v
                o[k + "_ref"] = fid[key]
            else:
                o[k] = v
        excluded.append(o)

    out = {}
    for k, v in full.items():
        if k == "excluded" and isinstance(v, list):
            out[k] = excluded
            if filters:
                out["filters"] = filters
        elif k == "rows":
            out["counts"] = counts
            out["page"] = pg
            out["columns"] = columns
            out["rows"] = out_rows
            if items or any("evidence" in r for r in rows):
                out["evidence"] = ev
                out["sources"] = sources
        else:
            out[k] = v
    if "rows" not in full:
        out.update({"counts": counts, "page": pg, "columns": columns})
    prefix = choose_prefix(full, full.get("graph_id"))
    return {"view": {"name": NAME, "of": "query", "version": VERSION, "legend": QUERY_LEGEND}, "id_prefix": prefix,
            **_shorten(out, prefix)}


def _expand_query(v: dict) -> dict:
    ev = v.get("evidence") or {}
    srcs = v.get("sources") or {}
    texts = {}

    def text_of(e: str) -> str:
        c = ev[e]
        for f in _TEXTS:
            if f in c:
                return c[f]
            if f + "_of" in c:
                return text_of(c[f + "_of"])
        raise ScaleError(f"evidence {e} has no text")

    for e, c in ev.items():
        full = {}
        for k, x in c.items():
            if k in ("quote_of", "transcription_of"):
                full[k[:-3]] = text_of(x)
            else:
                full[k] = x
        s = srcs.get(full.get("source")) if isinstance(full.get("source"), str) else None
        for f, short in _SOURCE_FIELDS.items():
            if s and short in s:
                full[f] = s[short]
        texts[e] = full
    columns = v.get("columns") or []
    rows = []
    for r in v.get("rows") or []:
        o = {}
        for k, x in r.items():
            if k == "values" and isinstance(x, list):
                o[k] = dict(zip(columns, x))
            elif k == "evidence" and isinstance(x, list):
                items = []
                for g in x:
                    rec, ent = _derived(r, g["var"])
                    rec = g["record"] if "record" in g else rec
                    ent = g["entry"] if "entry" in g else ent
                    for pair in g["items"].split():
                        i, e = pair.split(":", 1)
                        items.append({"var": g["var"], "record": rec, **({"entry": ent} if ent is not None else {}),
                                      "evidence": int(i), **texts[e]})
                o[k] = items
            else:
                o[k] = x
        rows.append(o)
    out = {k: x for k, x in v.items() if k not in _VIEW_KEYS}
    if "rows" in v:
        out["rows"] = rows
    if isinstance(v.get("excluded"), list):
        fs = v.get("filters") or {}
        out["excluded"] = [{(k[:-4] if k[:-4] in _FILTER_KEYS and k.endswith("_ref") else k):
                            (fs[x] if k[:-4] in _FILTER_KEYS and k.endswith("_ref") else x) for k, x in e.items()}
                           for e in v["excluded"]]
    return out


# ------------------------------------------------------------------------------------------------- search


def pack_search(full: dict, doc: dict | None = None) -> dict:
    """The model view of a search result (``scale_search``'s ``view: model``)."""
    if "error" in full:
        return full
    _check_keys(full)
    hits = full.get("hits") or []
    moved = _constant_source_fields(hits, ["source_year"])
    alsos = [a for h in hits for a in (h.get("also") or [])]
    by_label = all("record_name" in x for x in hits + alsos)
    out_hits = []
    for h in hits:
        o = {}
        for k, x in h.items():
            if k == "id" and x == f"{h.get('record')}#evidence/{h.get('evidence')}":
                continue
            if k == "source_year" and "source_year" in (moved.get(h.get("source")) or {}):
                continue
            if k == "also" and isinstance(x, list) and by_label:
                x = [{kk: vv for kk, vv in a.items() if not (kk == "record_name" and vv == a.get("label"))} for a in x]
            o[k] = x
        if "id" not in h:
            o["id_absent"] = True
        out_hits.append(o)
    srcs = sorted({h["source"] for h in hits if isinstance(h.get("source"), str)})
    names = _source_names(doc, set(srcs))
    sources = {s: {**({"year": moved[s]["source_year"]} if "source_year" in (moved.get(s) or {}) else {}),
                   **names.get(s, {})} for s in srcs}
    out = {"view": {"name": NAME, "of": "search", "version": VERSION, "legend": SEARCH_LEGEND,
                    "also_named_by_label": by_label}}
    prefix = choose_prefix(full)
    out["id_prefix"] = prefix
    body = {k: (out_hits if k == "hits" else x) for k, x in full.items()}
    body["sources"] = sources
    return {**out, **_shorten(body, prefix)}


def _expand_search(v: dict) -> dict:
    srcs = v.get("sources") or {}
    by_label = v["view"].get("also_named_by_label")
    hits = []
    for h in v.get("hits") or []:
        o = {}
        for k, x in h.items():
            if k == "id_absent":
                continue
            if k == "also" and isinstance(x, list) and by_label:
                x = [{**a, "record_name": a["record_name"] if "record_name" in a else a.get("label")} for a in x]
            o[k] = x
        if "id" not in h and not h.get("id_absent"):
            o["id"] = f"{h.get('record')}#evidence/{h.get('evidence')}"
        s = srcs.get(h.get("source")) if isinstance(h.get("source"), str) else None
        if s and "year" in s:
            o["source_year"] = s["year"]
        hits.append(o)
    out = {k: x for k, x in v.items() if k not in _VIEW_KEYS}
    out["hits"] = hits
    return out


# ------------------------------------------------------------------------------------------------- both


def expand(view: dict) -> dict:
    """The full result a model view was written from (a query view: with its page's rows only; key order aside)."""
    meta = view.get("view") if isinstance(view, dict) else None
    if not isinstance(meta, dict) or meta.get("name") != NAME:
        raise ScaleError("not a model view (no view.name model)")
    if meta.get("version") != VERSION:
        raise ScaleError(f"model view version {meta.get('version')!r}; this code reads version {VERSION}")
    v = _lengthen({k: x for k, x in view.items() if k not in ("view", "id_prefix")}, view.get("id_prefix"))
    v["view"] = meta
    if meta.get("of") == "query":
        return _expand_query(v)
    if meta.get("of") == "search":
        return _expand_search(v)
    raise ScaleError(f"a model view of {meta.get('of')!r}: expected query or search")
