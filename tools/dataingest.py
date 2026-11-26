"""``scale data add``: tabular measurements and computed results into a scale-catal document, by a declarative mapping.

A deterministic compiler. It reads one CSV file under a table profile (``scale_csv_v1``, :mod:`scale.datatable`) and a
mapping (YAML, ``mapping_version: 1``; unknown keys are refused; no code, no templates, nothing fetched) and writes into
a document that already holds what the rows refer to (the Variables, the products, the assays, the input datasets):

* one Dataset record for the file: ``data_kind: table``, its version or uri, the sha256 of its exact bytes, its table
  (profile, primary key, every column with its datatype and, for a quantity, its Variable, unit and basis, the number
  of rows) and what compiled it (``table.ingest``: the mapping's sha256, the tool, the schema version and its digest);
* one record per distinct record key (``record.key``), with a stable id (``record.id_prefix`` and the key's values),
  its origin (``recorded`` needs a supplied ``recorded_at``), the properties the mapping names (literal, or read from a
  column; a property that differs between the rows of one record is refused), and one result entry per row
  (``result``: its id, variable, unit, product, producer, phase, stream, basis);
* for every value read from a cell, its own evidence item: kind ``dataset_row``, the Dataset as source, a
  ``dataset_cell`` naming its column and its row by key (or, opted in, by position), the cell's text as the quote;
  the value cites it by ``evidence_ids``;
* the REPORTED_IN link of each record to the Dataset, and the links the mapping names.

A cell is a number written as the profile writes one, or a token the mapping names: ``missing`` maps a token (a blank
included) to a missing value (``not_reported`` with its reason, ``not_measured``, ``not_applicable``), ``censor`` a
prefix (``<``) to a one-sided bound or a detection or quantification limit. A blank, a failed calculation or a token is
never read as zero, and a cell that is none of these is refused. The source token stays in ``as_reported``.

Before anything is written the result is validated with the destination (quantity contracts included); an error the
destination did not have refuses the import. The same file, mapping, tool and schema give the same records: a retry
changes nothing, and a record that exists with other content is a conflict, never overwritten. New bytes are a new
Dataset (a new id and version, linked by UPDATES), never a new checksum on an old one.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__
from .datatable import parse_number, read_table, sha256
from .util import ScaleError, dump_canonical, load_doc

MAPPING_VERSION = 1
TOP_KEYS = {"mapping_version", "dataset", "record", "result", "links"}
DATASET_KEYS = {"id", "name", "version", "uri", "profile", "primary_key", "row_selector", "licence", "availability",
                "description", "columns"}
COLUMN_KEYS = {"datatype", "unit", "variable", "basis"}
RECORD_KEYS = {"label", "id_prefix", "key", "name", "name_column", "origin", "recorded_at", "recorded_at_column",
               "evidence_type", "properties"}
PROPERTY_KEYS = {"literal", "column", "ref_column", "unit", "representation", "basis", "definition", "missing", "censor"}
RESULT_KEYS = {"id_column", "column", "variable", "unit", "basis", "definition", "origin", "target", "target_column",
               "unresolved_target", "produced_by", "produced_by_column", "phase", "phase_column", "stream",
               "stream_column", "missing", "censor"}
LINK_KEYS = {"type", "target", "target_column", "props"}
CENSOR_KEYS = {"prefix", "as"}
CENSOR_AS = {"upper_bound": {"status": "present", "bound": "upper"},
             "lower_bound": {"status": "present", "bound": "lower"},
             "below_detection_limit": {"status": "below_detection_limit"},
             "below_quantification_limit": {"status": "below_quantification_limit"}}
MISSING_KEYS = {"status", "missing_reason", "note"}
MISSING_STATUSES = ("not_reported", "not_measured", "not_applicable")
ORIGINS = ("recorded", "computed", "reported", "derived", "curated", "synthetic")
REPRESENTATIONS = ("text_value", "boolean_value")
RESULT_ID = re.compile(r"^[a-z][a-z0-9_]*$")
DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
OMIT = object()


def _fail(msg: str) -> None:
    raise ScaleError(msg)


def _keys(obj: Any, allowed: set, where: str, required: tuple = ()) -> dict:
    if not isinstance(obj, dict):
        _fail(f"mapping: {where} is a mapping of keys")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        _fail(f"mapping: {where} has unknown keys {unknown} (allowed: {sorted(allowed)})")
    missing = [k for k in required if k not in obj]
    if missing:
        _fail(f"mapping: {where} needs {missing}")
    return obj


def _str(obj: dict, k: str, where: str) -> str | None:
    v = obj.get(k)
    if v is not None and (not isinstance(v, str) or not v):
        _fail(f"mapping: {where}.{k} is a non-empty string")
    return v


def _cols(obj: dict, k: str, where: str) -> list[str]:
    v = obj.get(k)
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) or len(set(v)) != len(v):
        _fail(f"mapping: {where}.{k} is a list of distinct column names")
    return v


def _missing(m: Any, where: str, raw: bool) -> dict:
    """A missing map: token -> the fields of a missing value (or ``omit`` for a property that is no value object)."""
    if m is None:
        return {}
    if not isinstance(m, dict):
        _fail(f"mapping: {where}.missing maps a token to a missing value")
    out = {}
    for tok, v in m.items():
        if not isinstance(tok, str):
            _fail(f"mapping: {where}.missing: the token {tok!r} is text (quote it; a blank is \"\")")
        if v == "omit":
            if not raw:
                _fail(f"mapping: {where}.missing[{tok!r}]: a value is recorded as missing, never left out (omit is only "
                      "for a property that is no value object)")
            out[tok] = OMIT
            continue
        if raw:
            _fail(f"mapping: {where}.missing[{tok!r}]: this property is no value object; a token can only be omitted")
        _keys(v, MISSING_KEYS, f"{where}.missing[{tok!r}]", ("status",))
        if v["status"] not in MISSING_STATUSES:
            _fail(f"mapping: {where}.missing[{tok!r}]: status is one of {list(MISSING_STATUSES)}; a token never becomes "
                  "a number (zero included)")
        if "missing_reason" in v and v["status"] != "not_reported":
            _fail(f"mapping: {where}.missing[{tok!r}]: missing_reason goes only with not_reported")
        out[tok] = dict(v)
    return out


def _censor(c: Any, where: str) -> list[dict]:
    if c is None:
        return []
    if not isinstance(c, list):
        _fail(f"mapping: {where}.censor is a list of {{prefix, as}}")
    out = []
    for i, x in enumerate(c):
        _keys(x, CENSOR_KEYS, f"{where}.censor[{i}]", ("prefix", "as"))
        if not isinstance(x["prefix"], str) or not x["prefix"] or x["as"] not in CENSOR_AS:
            _fail(f"mapping: {where}.censor[{i}]: prefix is text and as one of {sorted(CENSOR_AS)}")
        out.append(x)
    return out


# ------------------------------------------------------------------------------------------ the mapping
def load_mapping(data: bytes, where: str = "mapping") -> dict:
    """The mapping, checked for its shape (not yet against the file or the destination)."""
    import yaml
    try:
        m = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ScaleError(f"{where}: not a YAML mapping ({exc})") from exc
    _keys(m, TOP_KEYS, "the mapping", ("mapping_version", "dataset", "record"))
    if m["mapping_version"] != MAPPING_VERSION:
        _fail(f"mapping: mapping_version {m['mapping_version']!r} is not one this tool reads ({MAPPING_VERSION})")
    d = _keys(m["dataset"], DATASET_KEYS, "dataset", ("id", "profile"))
    for k in ("id", "name", "version", "uri", "licence", "description"):
        _str(d, k, "dataset")
    if not (d.get("version") or d.get("uri")):
        _fail("mapping: dataset names its version or its uri (a cell is found again only in the bytes cited)")
    if d.get("row_selector", "key") not in ("key", "index"):
        _fail("mapping: dataset.row_selector is key (the default) or index")
    if d.get("row_selector", "key") == "key":
        _cols(d, "primary_key", "dataset")
        if not d["primary_key"]:
            _fail("mapping: dataset.primary_key names at least one column (or set row_selector: index)")
    else:
        d.setdefault("primary_key", [])
        _cols(d, "primary_key", "dataset")
    if d.get("availability", "available") not in ("available", "restricted", "not_reported"):
        _fail("mapping: dataset.availability is available, restricted or not_reported")
    for name, c in (d.get("columns") or {}).items():
        _keys(c, COLUMN_KEYS, f"dataset.columns.{name}")
        if "datatype" in c and c["datatype"] not in ("string", "integer", "number", "boolean", "datetime"):
            _fail(f"mapping: dataset.columns.{name}.datatype is string, integer, number, boolean or datetime")
    r = _keys(m["record"], RECORD_KEYS, "record", ("label", "key", "origin"))
    _cols(r, "key", "record")
    if not r["key"]:
        _fail("mapping: record.key names at least one column")
    if r["origin"] not in ORIGINS:
        _fail(f"mapping: record.origin is one of {list(ORIGINS)}")
    if r["origin"] == "recorded" and not (r.get("recorded_at") or r.get("recorded_at_column")):
        _fail("mapping: a recorded record carries recorded_at: give recorded_at_column (or recorded_at); it is never "
              "the time of the import")
    if r.get("recorded_at") is not None and not (isinstance(r["recorded_at"], str) and DATETIME.fullmatch(r["recorded_at"])):
        _fail("mapping: record.recorded_at is an ISO 8601 date-time with a time zone")
    for k in ("label", "id_prefix", "name", "name_column", "recorded_at_column", "evidence_type"):
        _str(r, k, "record")
    for prop, p in (r.get("properties") or {}).items():
        where = f"record.properties.{prop}"
        _keys(p, PROPERTY_KEYS, where)
        sources = [k for k in ("literal", "column", "ref_column") if k in p]
        if len(sources) != 1:
            _fail(f"mapping: {where} takes exactly one of literal, column and ref_column")
        if "representation" in p and p["representation"] not in REPRESENTATIONS:
            _fail(f"mapping: {where}.representation is one of {list(REPRESENTATIONS)}")
        _censor(p.get("censor"), where)
    if "result" in m:
        s = _keys(m["result"], RESULT_KEYS, "result", ("id_column", "column", "variable"))
        for k in ("target", "target_column", "unresolved_target"):
            _str(s, k, "result")
        if sum(k in s for k in ("target", "target_column", "unresolved_target")) > 1:
            _fail("mapping: result takes at most one of target, target_column and unresolved_target")
        if "produced_by" in s and "produced_by_column" in s:
            _fail("mapping: result takes produced_by or produced_by_column, not both")
        for k in ("phase", "stream"):
            if k in s and f"{k}_column" in s:
                _fail(f"mapping: result takes {k} or {k}_column, not both")
        _missing(s.get("missing"), "result", False)
        _censor(s.get("censor"), "result")
    for i, ln in enumerate(m.get("links") or []):
        _keys(ln, LINK_KEYS, f"links[{i}]", ("type",))
        if ("target" in ln) == ("target_column" in ln):
            _fail(f"mapping: links[{i}] takes exactly one of target and target_column")
        if "props" in ln and not isinstance(ln["props"], dict):
            _fail(f"mapping: links[{i}].props is a mapping")
    return m


# ------------------------------------------------------------------------------------------ compiling
def _seg(text: str) -> str:
    """One segment of a generated id: the cell's text, percent-encoded (``/`` and ``,`` included) so that two texts,
    and two keys joined by ``/`` or ``,``, never give one id."""
    return quote(text, safe="")


class _Compiler:
    def __init__(self, m: dict, header: list[str], body: list[list[str]], doc: dict, spec: dict):
        self.m, self.header, self.body, self.doc, self.spec = m, header, body, doc, spec
        self.d, self.r, self.s = m["dataset"], m["record"], m.get("result")
        self.did = self.d["id"]
        self.by_key = self.d.get("row_selector", "key") == "key"
        self.pk = list(self.d.get("primary_key") or [])
        self.numeric: dict[str, dict] = {}          # column -> what it holds (unit, variable, basis), as numbers
        self.textual: set[str] = set()
        self.refs: dict[str, str] = {}              # an id the records name -> where
        self.tokens: dict[str, set] = {}            # column -> the missing tokens its mapping names
        self.prefixes: dict[str, tuple] = {}        # column -> the censoring prefixes its mapping names
        synthetic = (doc.get("graph") or {}).get("data_status") == "synthetic_example"
        if synthetic and self.r["origin"] != "synthetic":
            _fail(f"the destination is a synthetic example (data_status synthetic_example), whose records are all "
                  f"origin synthetic; the mapping says {self.r['origin']}")
        if not synthetic and self.r["origin"] == "synthetic":
            _fail("record.origin synthetic goes only into a synthetic example document")
        self.link_origin = "synthetic" if synthetic else "curated"

    def col(self, name: str, where: str) -> int:
        if name not in self.header:
            _fail(f"mapping: {where} names the column {name!r}, which the file's header does not have ({self.header})")
        return self.header.index(name)

    # ---- the rows, by key
    def rows(self) -> list[tuple[int, list[str]]]:
        rows = list(enumerate(self.body))
        if not self.pk:
            return rows
        pos = [self.col(c, "dataset.primary_key") for c in self.pk]
        seen: dict[tuple, int] = {}
        for i, row in rows:
            key = tuple(row[p] for p in pos)
            blank = [c for c, v in zip(self.pk, key) if v == ""]
            if blank:
                _fail(f"data row {i}: the primary-key column {blank[0]!r} is blank")
            if key in seen:
                _fail(f"duplicate key: data rows {seen[key]} and {i} both have {dict(zip(self.pk, key))} "
                      f"(primary_key {self.pk})")
            seen[key] = i
        if not self.by_key:
            return rows                             # cells are named by position: the file's order
        return sorted(rows, key=lambda x: tuple(x[1][p] for p in pos))   # the order of the keys, never of the file

    def row_cell(self, i: int, row: list[str], column: str) -> dict:
        if self.by_key:
            return {"column": column, "row_key": [{"column": c, "value": row[self.header.index(c)]} for c in self.pk]}
        return {"column": column, "row_index": i}

    def row_tag(self, i: int, row: list[str]) -> str:
        if self.by_key:
            return ",".join(_seg(row[self.header.index(c)]) for c in self.pk)   # "," is encoded in a segment
        return f"row{i}"

    def row_desc(self, i: int, row: list[str]) -> str:
        if self.by_key:
            return "row " + ", ".join(f"{c}={row[self.header.index(c)]}" for c in self.pk)
        return f"row index {i}"

    def evidence(self, i: int, row: list[str], column: str) -> tuple[str, dict]:
        text = row[self.col(column, "the mapping")]
        eid = f"{column}.{self.row_tag(i, row)}"
        item = {"id": eid, "source": self.did, "kind": "dataset_row",
                "locator": f"{self.row_desc(i, row)}; column {column}", "dataset_cell": self.row_cell(i, row, column)}
        if text:
            item["quote"] = text
        if self.r.get("evidence_type"):
            item["evidence_type"] = self.r["evidence_type"]
        return eid, item

    # ---- cells
    def number(self, text: str, unit: str, censor: list[dict], where: str) -> dict:
        for c in censor:
            if text.startswith(c["prefix"]):
                num = parse_number(text[len(c["prefix"]):])
                if num is None:
                    _fail(f"{where}: {text!r} is not a number after its censoring prefix {c['prefix']!r}")
                return {**CENSOR_AS[c["as"]], "value": num, "unit": unit, "as_reported": text}
        num = parse_number(text)
        if num is None:
            _fail(f"{where}: {text!r} is not a number" + (" (a blank cell: map \"\" under missing)" if text == "" else
                                                         "; map the token under missing or censor, or correct the file"))
        return {"status": "present", "value": num, "unit": unit}

    def value(self, text: str, p: dict, where: str, raw: bool, column: str | None = None) -> Any:
        """The property or result value a cell gives, or OMIT."""
        missing = _missing(p.get("missing"), where, raw)
        if column:
            self.tokens.setdefault(column, set()).update(missing)
            self.prefixes[column] = tuple(c["prefix"] for c in _censor(p.get("censor"), where))
        if text in missing:
            if missing[text] is OMIT:
                return OMIT
            return {**missing[text], **({"as_reported": text} if text else {})}
        if raw:
            if text == "":
                _fail(f"{where}: a blank cell (map \"\" under missing as omit, or fill the file)")
            return text
        if p.get("unit") is not None:
            return self.number(text, p["unit"], _censor(p.get("censor"), where), where)
        rep = p.get("representation")
        if text == "":
            _fail(f"{where}: a blank cell (map \"\" under missing)")
        if rep == "boolean_value":
            if text.lower() not in ("true", "false"):
                _fail(f"{where}: {text!r} is not true or false")
            return {"status": "present", "boolean_value": text.lower() == "true"}
        return {"status": "present", "text_value": text}

    def prop_type(self, prop: str) -> dict:
        from .quantities import prop_def
        d = prop_def(self.spec, {"label": self.r["label"], "props": {}}, prop)
        if not isinstance(d, dict):
            _fail(f"mapping: record.properties.{prop} is not a property of {self.r['label']}")
        return d

    def prop_value(self, prop: str, p: dict, i: int, row: list[str]) -> tuple[Any, tuple | None]:
        """(the value, (evidence id, item) or None) a row gives a property."""
        where = f"data row {i}, property {prop}"
        if "literal" in p:
            return copy.deepcopy(p["literal"]), None
        d = self.prop_type(prop)
        t = d.get("type")
        column = p.get("column") or p.get("ref_column")
        text = row[self.col(column, f"record.properties.{prop}")]
        if "ref_column" in p:
            if t not in ("ref",):
                _fail(f"mapping: record.properties.{prop}: ref_column goes with a reference, and {prop} is a {t}")
            v = self.value(text, p, where, raw=True, column=column)
            if v is not OMIT:
                self.refs.setdefault(v, f"{where} (column {column})")
                self.textual.add(column)
        elif t == "value":
            if p.get("unit") is None and p.get("representation") is None:
                _fail(f"mapping: record.properties.{prop} is a value: give its unit (a number) or its representation "
                      "(text_value, boolean_value)")
            v = self.value(text, p, where, raw=False, column=column)
            if isinstance(v, dict):
                for k in ("basis", "definition"):
                    if p.get(k):
                        v[k] = p[k]
            if p.get("unit") is not None:
                self.numeric.setdefault(column, {"unit": p["unit"], **({"basis": p["basis"]} if p.get("basis") else {})})
            else:
                self.textual.add(column)
        elif t in ("string", "text", "enum", "date", "datetime", "integer", "number", "boolean"):
            if p.get("unit") or p.get("representation"):
                _fail(f"mapping: record.properties.{prop} is a {t}, not a value object: it takes no unit or representation")
            v = self.value(text, p, where, raw=True, column=column)
            if v is not OMIT and t in ("integer", "number"):
                n = parse_number(v)
                if n is None or (t == "integer" and not isinstance(n, int)):
                    _fail(f"{where}: {v!r} is not an {t}")
                v = n
            elif v is not OMIT and t == "boolean":
                if v.lower() not in ("true", "false"):
                    _fail(f"{where}: {v!r} is not true or false")
                v = v.lower() == "true"
            self.textual.add(column)
        else:
            _fail(f"mapping: record.properties.{prop} is a {t}: give it as a literal"
                  + (" or read it with ref_column" if t == "ref" else ""))
        if v is OMIT:
            return OMIT, None
        ev = self.evidence(i, row, column)
        if isinstance(v, dict) and "status" in v:
            v["evidence_ids"] = [ev[0]]
        return v, ev

    def result(self, i: int, row: list[str]) -> tuple[dict, list[tuple]]:
        s = self.s
        where = f"data row {i}, result"
        rid = row[self.col(s["id_column"], "result.id_column")]
        if not RESULT_ID.fullmatch(rid):
            _fail(f"{where}: the result id {rid!r} (column {s['id_column']}) is not snake_case (lower-case letters, "
                  "digits and _, starting with a letter)")
        if s.get("unit") is None:
            _fail("mapping: result.unit is the unit of the result column")
        v = self.value(row[self.col(s["column"], "result.column")], s, where, raw=False, column=s["column"])
        eid, item = self.evidence(i, row, s["column"])
        out = {"id": rid, **v, "variable": s["variable"], "evidence_ids": [eid]}
        self.refs.setdefault(s["variable"], "result.variable")
        evs = [(eid, item)]
        if s.get("target"):
            out["target"] = s["target"]
            self.refs.setdefault(s["target"], "result.target")
        elif s.get("target_column"):
            t = row[self.col(s["target_column"], "result.target_column")]
            if not t:
                _fail(f"{where}: the product (column {s['target_column']}) is blank")
            out["target"] = t
            self.refs.setdefault(t, f"{where} (column {s['target_column']})")
            evs.append(self.evidence(i, row, s["target_column"]))
            self.textual.add(s["target_column"])
        elif s.get("unresolved_target"):
            out["unresolved_target"] = s["unresolved_target"]
        prod = s.get("produced_by")
        if prod and prod != "self":
            out["produced_by"] = prod
            self.refs.setdefault(prod, "result.produced_by")
        elif s.get("produced_by_column"):
            t = row[self.col(s["produced_by_column"], "result.produced_by_column")]
            if not t:
                _fail(f"{where}: the producer (column {s['produced_by_column']}) is blank")
            out["produced_by"] = t
            self.refs.setdefault(t, f"{where} (column {s['produced_by_column']})")
            evs.append(self.evidence(i, row, s["produced_by_column"]))
            self.textual.add(s["produced_by_column"])
        for k in ("phase", "stream"):
            if s.get(k):
                out[k] = s[k]
            elif s.get(f"{k}_column"):
                t = row[self.col(s[f"{k}_column"], f"result.{k}_column")]
                if t:
                    out[k] = t
                    evs.append(self.evidence(i, row, s[f"{k}_column"]))
                    self.textual.add(s[f"{k}_column"])
        for k in ("basis", "definition", "origin"):
            if s.get(k):
                out[k] = s[k]
        self.numeric[s["column"]] = {"unit": s["unit"], "variable": s["variable"],
                                     **({"basis": s["basis"]} if s.get("basis") else {})}
        out["evidence_ids"] = [e[0] for e in evs]      # the cells that state it: its number and what identifies it
        return out, evs

    def compile(self) -> tuple[dict, list[dict], list[dict]]:
        r = self.r
        rows = self.rows()
        kpos = [self.col(c, "record.key") for c in r["key"]]
        prefix = r.get("id_prefix") or f"{self.did}/{r['label'].lower()}/"
        groups: dict[tuple, list[tuple[int, list[str]]]] = {}
        for i, row in rows:
            key = tuple(row[p] for p in kpos)
            blank = [c for c, v in zip(r["key"], key) if v == ""]
            if blank:
                _fail(f"data row {i}: the record key column {blank[0]!r} is blank")
            groups.setdefault(key, []).append((i, row))
        nodes, links = [], []
        for key, members in sorted(groups.items()):
            rid = prefix + "/".join(_seg(v) for v in key)
            props: dict = {"origin": r["origin"]}
            evidence: dict[str, dict] = {}
            # the record's name and recording time: one per record
            for what, col in (("name", r.get("name_column")), ("recorded_at", r.get("recorded_at_column"))):
                if col:
                    got = {row[self.col(col, f'record.{what}_column')] for _, row in members}
                    if len(got) > 1:
                        _fail(f"conflicting run-level property {what} for {rid}: column {col} holds {sorted(got)}")
                    val = got.pop()
                    if not val:
                        _fail(f"{rid}: {what} (column {col}) is blank")
                    if what == "recorded_at" and not DATETIME.fullmatch(val):
                        _fail(f"{rid}: recorded_at {val!r} (column {col}) is not an ISO 8601 date-time with a time zone")
                    props[what] = val
            if "name" in props and r.get("name"):            # a name and a name column: the two together
                props["name"] = f"{r['name']}, {props['name']}"
            if "name" not in props:
                props["name"] = f"{r.get('name') or r['label']}, " + ", ".join(f"{c}={v}" for c, v in zip(r["key"], key))
            if "recorded_at" not in props and r.get("recorded_at"):
                props["recorded_at"] = r["recorded_at"]
            for prop in sorted(r.get("properties") or {}):
                p = r["properties"][prop]
                got = [(i, *self.prop_value(prop, p, i, row)) for i, row in members]
                seen = {json.dumps(_without_evidence(v), sort_keys=True, ensure_ascii=False) for _, v, _ in got}
                if len(seen) > 1:
                    _fail(f"conflicting run-level property {prop} for {rid}: its rows give "
                          + "; ".join(f"row {i}: {json.dumps(_without_evidence(v), ensure_ascii=False)}"
                                      for i, v, _ in got[:4]))
                _, v, ev = got[0]                            # the first row by key cites the cell
                if v is OMIT:
                    continue
                props[prop] = v
                if ev:
                    _put(evidence, *ev)
            if self.s:
                results = []
                for i, row in members:
                    entry, evs = self.result(i, row)
                    if any(x["id"] == entry["id"] for x in results):
                        _fail(f"duplicate key: {rid} has two results {entry['id']!r} (column {self.s['id_column']})")
                    results.append(entry)
                    for ev in evs:
                        _put(evidence, *ev)
                props["result_values"] = sorted(results, key=lambda x: x["id"])
            props["evidence"] = [evidence[k] for k in sorted(evidence)]
            nodes.append({"id": rid, "label": r["label"], "props": props})
            for j, ln in enumerate(self.m.get("links") or []):
                if "target" in ln:
                    tgt, ev = ln["target"], None
                else:
                    got = {row[self.col(ln["target_column"], f"links[{j}].target_column")] for _, row in members}
                    if len(got) > 1:
                        _fail(f"conflicting run-level property links[{j}] ({ln['type']}) for {rid}: column "
                              f"{ln['target_column']} holds {sorted(got)}")
                    tgt = got.pop()
                    if not tgt:
                        _fail(f"{rid}: links[{j}] ({ln['type']}): the target (column {ln['target_column']}) is blank")
                    ev = self.evidence(*members[0], ln["target_column"])
                    self.textual.add(ln["target_column"])
                self.refs.setdefault(tgt, f"links[{j}]")
                lp = {"origin": self.link_origin, **copy.deepcopy(ln.get("props") or {})}
                if ev:
                    lp["evidence"] = [ev[1]]
                links.append({"id": f"{rid}/{ln['type'].lower().replace('_', '-')}/{_seg(tgt)}", "source": rid,
                              "target": tgt, "type": ln["type"], "props": lp})
            rows_cited = sorted({e["locator"].split(";")[0] for e in evidence.values()})
            links.append({"id": f"{rid}/reported-in/{self.did.rsplit('/', 1)[-1]}", "source": rid, "target": self.did,
                          "type": "REPORTED_IN", "props": {"origin": self.link_origin, "locators": rows_cited}})
        return self.dataset(), nodes, links

    def dataset(self) -> dict:
        over = self.d.get("columns") or {}
        unknown = sorted(set(over) - set(self.header))
        if unknown:
            _fail(f"mapping: dataset.columns names {unknown}, which the file's header does not have")
        dt = {c: "datetime" for c in [self.r.get("recorded_at_column")] if c}
        cols = []
        for name in self.header:
            c = {"name": name, "datatype": dt.get(name, "number" if name in self.numeric else "string"),
                 **self.numeric.get(name, {})}
            c.update(over.get(name) or {})
            if c.get("variable"):
                self.refs.setdefault(c["variable"], f"dataset.columns.{name}.variable")
            cols.append(c)
        for c in cols:                              # every cell is of its column's datatype, or a token the mapping names
            j, tokens = self.header.index(c["name"]), self.tokens.get(c["name"], set())
            for i, row in enumerate(self.body):
                text = row[j]
                if text == "" or text in tokens or any(text.startswith(p) for p in self.prefixes.get(c["name"], ())):
                    continue
                if not _of_type(text, c["datatype"]):
                    _fail(f"data row {i}: column {c['name']!r} is declared {c['datatype']}, and its cell {text!r} is "
                          "not one (nor a token the mapping names)")
        return {"profile": self.d["profile"], "primary_key": list(self.pk), "columns": cols, "rows": len(self.body)}


def _of_type(text: str, datatype: str) -> bool:
    if datatype == "number":
        return parse_number(text) is not None
    if datatype == "integer":
        return isinstance(parse_number(text), int)
    if datatype == "boolean":
        return text.lower() in ("true", "false")
    if datatype == "datetime":
        return bool(DATETIME.fullmatch(text))
    return True


def _put(evidence: dict, eid: str, item: dict) -> None:
    """Add an evidence item; an id that names another cell is refused (one id, one cell)."""
    if eid in evidence and evidence[eid] != item:
        _fail(f"two cells give the evidence id {eid!r} ({evidence[eid]['locator']}; {item['locator']})")
    evidence.setdefault(eid, item)


def _without_evidence(v: Any) -> Any:
    return {k: x for k, x in v.items() if k != "evidence_ids"} if isinstance(v, dict) else (
        None if v is OMIT else v)


# ------------------------------------------------------------------------------------------ the import
def plan(data: bytes, mapping: bytes, doc: dict, spec: dict, file_name: str = "the file") -> dict:
    """What importing ``data`` (a CSV file's bytes) by ``mapping`` (YAML bytes) into ``doc`` (validated under ``spec``)
    adds: ``{document, added, unchanged, dataset, records, results, validation, warnings}``. Refuses (ScaleError) a
    malformed mapping or file, duplicate keys, conflicting run-level properties, unresolved references, malformed numbers,
    a conflict with what the document holds and any validation error the import would introduce."""
    from . import pgraph
    if not isinstance(spec.get("dataset_cell"), dict):
        _fail(f"the destination pins scale-catal {spec.get('version')}, which has no dataset cells (0.8.4 on); "
              "migrate it first (scale graph migrate)")
    m = load_mapping(mapping)
    header, body = read_table(data, m["dataset"]["profile"], file_name)
    comp = _Compiler(m, header, body, doc, spec)
    table, nodes, links = comp.compile()
    have = {n.get("id"): n for n in doc.get("nodes") or []} | {e.get("id"): e for e in doc.get("links") or []}
    generated = {n["id"] for n in nodes} | {comp.did}
    unresolved = sorted((x, w) for x, w in comp.refs.items() if x not in have and x not in generated)
    if unresolved:
        _fail("unresolved references (the destination must hold what the rows name): "
              + "; ".join(f"{x} ({w})" for x, w in unresolved[:20]))
    digest = sha256(data)
    old = have.get(comp.did)
    if old is not None and (old.get("label") != "Dataset" or (old.get("props") or {}).get("sha256") != digest):
        _fail(f"{comp.did} is already in the document with other bytes (sha256 "
              f"{(old.get('props') or {}).get('sha256')}); new bytes are a new Dataset: give it a new id and version, "
              "and link it to this one with UPDATES")
    d = m["dataset"]
    table["ingest"] = {"mapping_sha256": sha256(mapping), "tool": f"scale-catal {__version__}",
                       "schema_version": str(spec.get("version")), "schema_sha256": pgraph.spec_sha256(spec)}
    ds = {"id": comp.did, "label": "Dataset", "props": {
        "name": d.get("name") or file_name, "data_kind": "table", "availability": d.get("availability", "available"),
        "sha256": digest, "media_type": "text/csv", "table": table,
        "origin": "synthetic" if m["record"]["origin"] == "synthetic" else "curated",
        **{k: d[k] for k in ("version", "uri", "licence", "description") if d.get(k)}}}
    new = [ds, *nodes]
    added, unchanged, conflicts = [], [], []
    for x in [*new, *links]:
        cur = have.get(x["id"])
        if cur is None:
            added.append(x)
        elif _same(cur, x):
            unchanged.append(x["id"])
        else:
            conflicts.append(x["id"])
    if conflicts:
        _fail(f"{len(conflicts)} records the import writes are already in the document with other content "
              f"({', '.join(conflicts[:8])}{' ...' if len(conflicts) > 8 else ''}); an import never overwrites a record: "
              "remove them first, or import new data as a new Dataset")
    out = copy.deepcopy(doc)
    out["nodes"] = list(out.get("nodes") or []) + [x for x in added if "label" in x]
    out["links"] = list(out.get("links") or []) + [x for x in added if "type" in x and "label" not in x]
    before = pgraph.validate(doc, spec)
    after = pgraph.validate(out, spec)
    old_err = _messages(before.errors)
    fresh = [e for e in after.errors if not _consume(old_err, e)]
    if fresh:
        _fail("the import would make the document invalid:\n" + "\n".join(f"  {e['record']}: {e['message']}"
                                                                         for e in fresh[:30]))
    old_warn = _messages(before.warnings)
    warnings = [e for e in after.warnings if not _consume(old_warn, e)]
    ours = {n["id"] for n in nodes} | {comp.did}
    conf = {}
    for ref, c in ((after.conformance or {}).get("profiles") or {}).items():
        mine = [v for v in c.get("violations") or [] if v.get("record") in ours]
        conf[ref] = {"violations": len(mine), "first": mine[:10]}
    return {"document": out, "added": [x["id"] for x in added], "unchanged": unchanged, "dataset": comp.did,
            "sha256": digest, "records": len(nodes), "results": sum(len(n["props"].get("result_values") or [])
                                                                   for n in nodes),
            "validation": {"valid": after.ok, "errors": len(after.errors), "new_warnings": len(warnings)},
            "warnings": warnings[:50], "checklist": conf, "nodes": [x for x in added if "label" in x],
            "links": [x for x in added if "label" not in x]}


def _same(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def _messages(items: list[dict]) -> dict:
    out: dict = {}
    for e in items:
        k = (e.get("record"), e.get("message"))
        out[k] = out.get(k, 0) + 1
    return out


def _consume(have: dict, e: dict) -> bool:
    k = (e.get("record"), e.get("message"))
    if have.get(k):
        have[k] -= 1
        return True
    return False


def add(csv_path: str | Path, map_path: str | Path, into: str | Path, spec: dict | None = None, repo: Path | None = None,
        dry_run: bool = False) -> dict:
    """Import ``csv_path`` by ``map_path`` into the document ``into`` (a JSON file), atomically; with ``dry_run``
    nothing is written. Returns the plan without the whole document."""
    from . import pgraph
    from .util import find_repo_root
    into = Path(into)
    if not into.is_file() or into.suffix != ".json":
        _fail(f"{into}: the destination is an existing scale-catal document (a .json file); it holds the Variables, "
              "products and assays the rows name")
    doc = load_doc(into)
    if spec is None:
        root = repo or find_repo_root()
        spec = pgraph.load_spec(pgraph.spec_for(root, (doc.get("graph") or {}).get("schema_version"), None, doc))
    data, mapping = Path(csv_path).read_bytes(), Path(map_path).read_bytes()
    res = plan(data, mapping, doc, spec, Path(csv_path).name)
    out = res.pop("document")
    res["dry_run"] = dry_run
    res["written"] = None
    if not dry_run and res["added"]:
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=f".{into.name}.", suffix=".tmp", dir=into.parent)   # a new file, never a link
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(dump_canonical(out))
            os.replace(tmp, into)                   # all or nothing: a retry after a failure starts from the same file
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        res["written"] = str(into)
    if not dry_run:
        res.pop("nodes"), res.pop("links")
    return res
