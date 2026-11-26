"""``scale data add``: tabular measurements and computed results into a scale-catal document, by a declarative mapping.

A deterministic compiler. It reads one table — a CSV file under a table profile (``scale_csv_v1``,
:mod:`scale.datatable`), or, with a mapping's ``input`` block (``mapping_version: 2``), a sheet of an Excel workbook or a
CSV file of another dialect (:mod:`scale.datainput`) — and a mapping (YAML; unknown keys are refused; no code, nothing
fetched), and writes into a document:

* one Dataset record for the table: ``data_kind: table``, its version or uri, the sha256 of its exact bytes, its table
  (profile, primary key, every column with its datatype and, for a quantity, its Variable, unit and basis, the number
  of rows) and what compiled it (``table.ingest``: the mapping's sha256, the tool, the schema version and its digest).
  A table read from a workbook or another dialect is written as ``scale_csv_v1``: the Dataset the cells name holds
  those canonical bytes, a second Dataset records the original file (its own sha256 and media type) and INCLUDES it,
  and ``table.ingest`` records how it was read (``input_*``: the sheet, the header row, the delimiter, the encoding;
  ``extracted_from``), so that ``scale data verify`` extracts the same bytes again from the original;
* one record per distinct record key of each record a mapping names (``mapping_version: 2`` may name several, a
  CatalystSample and the CatalyticTests that use it), with a stable id, its origin (``recorded`` needs a supplied
  ``recorded_at``), the properties the mapping names (literal, or read from a column; a property that differs between
  the rows of one record is refused), and its result entries (one per row, or, with ``melt``, one per row and column
  of a wide table);
* for every value read from a cell, its own evidence item: kind ``dataset_row``, the Dataset as source, a
  ``dataset_cell`` naming its column and its row by key (or, opted in, by position), the cell's text as the quote;
  the value cites it by ``evidence_ids``;
* the REPORTED_IN link of each record to the Dataset, and the links the mapping names;
* in ``mapping_version: 2``, the reference records the mapping declares and the rows use, when the document lacks them
  (``references``: a Variable, reused when the document holds exactly one of the kind) and the records a ``lookup``
  may create (a ChemicalSpecies from its formula); a lookup turns the names in cells into record ids, and the table
  records each name it resolved (``table.ingest``: ``lookup_<n>``), so that verification reads the cell as the id.

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

MAPPING_VERSIONS = (1, 2)
MAPPING_VERSION = 2                     # the newest this tool reads (it reads 1 as well, unchanged)
TOP_KEYS = {"mapping_version", "dataset", "record", "result", "links"}
TOP_KEYS_2 = TOP_KEYS | {"input", "records", "references", "lookups"}
DATASET_KEYS = {"id", "name", "version", "uri", "profile", "primary_key", "row_selector", "licence", "availability",
                "description", "columns"}
DATASET_KEYS_2 = DATASET_KEYS | {"id_prefix", "file_id"}
COLUMN_KEYS = {"datatype", "unit", "variable", "basis"}
RECORD_KEYS = {"label", "id_prefix", "key", "name", "name_column", "origin", "recorded_at", "recorded_at_column",
               "evidence_type", "properties"}
RECORD_KEYS_2 = RECORD_KEYS | {"as", "results", "links"}
PROPERTY_KEYS = {"literal", "column", "ref_column", "unit", "representation", "basis", "definition", "missing", "censor"}
PROPERTY_KEYS_2 = PROPERTY_KEYS | {"lookup", "unit_from_header"}
RESULT_KEYS = {"id_column", "column", "variable", "unit", "basis", "definition", "origin", "target", "target_column",
               "unresolved_target", "produced_by", "produced_by_column", "phase", "phase_column", "stream",
               "stream_column", "missing", "censor"}
RESULT_KEYS_2 = RESULT_KEYS | {"id", "id_slug", "melt", "target_lookup", "unit_from_header", "basis_column",
                               "unresolved_target_column"}
MELT_KEYS = {"column", "id", "target", "unresolved_target", "unit", "definition", "phase", "stream"}
LINK_KEYS = {"type", "target", "target_column", "props"}
LINK_KEYS_2 = LINK_KEYS | {"target_lookup", "target_record"}
REFERENCE_KEYS = {"id", "label", "props", "reuse"}
LOOKUP_KEYS = {"label", "values", "aliases", "match", "create"}
CREATE_KEYS = {"id_prefix", "as", "props"}
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
HANDLE = re.compile(r"^[a-z][a-z0-9_]*$")
DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
# a unit at the end of a header: "FE CO (%)", "T [K]", "T / °C" (a slash with spaces around it)
HEADER_UNIT = (re.compile(r"^(?P<name>.*?)\s*\((?P<unit>[^()]+)\)\s*$"),
               re.compile(r"^(?P<name>.*?)\s*\[(?P<unit>[^\[\]]+)\]\s*$"),
               re.compile(r"^(?P<name>.*?\S)\s+/\s+(?P<unit>\S.*?)\s*$"))
OMIT = object()


def _fail(msg: str) -> None:
    raise ScaleError(msg)


def header_unit(header: str) -> tuple[str, str | None]:
    """(the name, the unit) a column header writes: ``FE CO (%)`` -> (FE CO, %), ``T / °C`` -> (T, °C); no unit -> None."""
    for p in HEADER_UNIT:
        m = p.match(header)
        if m and m.group("unit").strip():
            return m.group("name").strip(), m.group("unit").strip()
    return header.strip(), None


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
    """The mapping, checked for its shape (not yet against the file or the destination). Version 1 and version 2 are
    read; either is returned with ``records`` (each with its ``results`` and ``links``)."""
    import yaml
    try:
        m = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ScaleError(f"{where}: not a YAML mapping ({exc})") from exc
    if not isinstance(m, dict):
        _fail("mapping: the mapping is a mapping of keys")
    v = m.get("mapping_version")
    if v not in MAPPING_VERSIONS:
        if "mapping_version" not in m:
            _keys(m, TOP_KEYS, "the mapping", ("mapping_version", "dataset", "record"))
        _fail(f"mapping: mapping_version {v!r} is not one this tool reads "
              f"({', '.join(str(x) for x in MAPPING_VERSIONS)})")
    two = v == 2
    _keys(m, TOP_KEYS_2 if two else TOP_KEYS, "the mapping", ("mapping_version", "dataset"))
    if not two and "record" not in m:
        _fail("mapping: the mapping needs ['record']")
    if two and ("records" in m) == ("record" in m):
        _fail("mapping: a version 2 mapping names its records in records (a list), or one record in record")
    if two and "records" in m and ("result" in m or "links" in m):
        _fail("mapping: with records, each record names its own results and links")
    d = _keys(m["dataset"], DATASET_KEYS_2 if two else DATASET_KEYS, "dataset", () if two else ("id", "profile"))
    for k in ("id", "name", "version", "uri", "licence", "description", "id_prefix", "file_id"):
        _str(d, k, "dataset")
    if two:
        if ("id" in d) == ("id_prefix" in d):
            _fail("mapping: dataset names its id, or an id_prefix (the id is then made of the prefix, the file's name "
                  "and its sha256, and, for a table extracted from it, the table's sha256)")
        d.setdefault("profile", "scale_csv_v1")
    elif not (d.get("version") or d.get("uri")):
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
    if two and "input" in m:
        from .datainput import RECIPE_KEYS
        _keys(m["input"], set(RECIPE_KEYS), "input")
    if two:
        _references(m.get("references"))
        _lookups(m.get("lookups"))
    if "records" in m:
        if not isinstance(m["records"], list) or not m["records"]:
            _fail("mapping: records is a list of one or more records")
        records = [_record(r, f"records[{i}]", two) for i, r in enumerate(m["records"])]
    else:
        r = dict(m["record"]) if isinstance(m["record"], dict) else m["record"]
        rec = _record(r, "record", two, top_result=m.get("result"), top_links=m.get("links"))
        records = [rec]
    handles = [r.get("as") for r in records if r.get("as")]
    if len(set(handles)) != len(handles):
        _fail("mapping: two records have one as")
    if two and len(records) > 1 and len(handles) != len(records):
        _fail("mapping: with several records, each names its as (a handle for its ids and links)")
    for r in records:
        for j, ln in enumerate(r["links"]):
            if ln.get("target_record") and ln["target_record"] not in handles:
                _fail(f"mapping: {r['_where']}.links[{j}].target_record {ln['target_record']!r} is the as of none of "
                      "the mapping's records")
    m["records"] = records
    m["_two"] = two
    return m


def _record(r: Any, where: str, two: bool, top_result: Any = None, top_links: Any = None) -> dict:
    _keys(r, RECORD_KEYS_2 if two else RECORD_KEYS, where, ("label", "key", "origin"))
    r = dict(r)
    if top_result is not None and "results" in r:
        _fail(f"mapping: {where} takes result (top level) or results, not both")
    if top_links is not None and "links" in r:
        _fail(f"mapping: {where} takes links (top level) or its own links, not both")
    _cols(r, "key", where)
    if not r["key"]:
        _fail(f"mapping: {where}.key names at least one column")
    if r["origin"] not in ORIGINS:
        _fail(f"mapping: {where}.origin is one of {list(ORIGINS)}")
    if r["origin"] == "recorded" and not (r.get("recorded_at") or r.get("recorded_at_column")):
        _fail("mapping: a recorded record carries recorded_at: give recorded_at_column (or recorded_at); it is never "
              "the time of the import")
    if r.get("recorded_at") is not None and not (isinstance(r["recorded_at"], str) and DATETIME.fullmatch(r["recorded_at"])):
        _fail(f"mapping: {where}.recorded_at is an ISO 8601 date-time with a time zone")
    for k in ("label", "id_prefix", "name", "name_column", "recorded_at_column", "evidence_type", "as"):
        _str(r, k, where)
    if r.get("as") is not None and not HANDLE.fullmatch(r["as"]):
        _fail(f"mapping: {where}.as is snake_case")
    for prop, p in (r.get("properties") or {}).items():
        pw = f"{where}.properties.{prop}"
        _keys(p, PROPERTY_KEYS_2 if two else PROPERTY_KEYS, pw)
        sources = [k for k in ("literal", "column", "ref_column") if k in p]
        if len(sources) != 1:
            _fail(f"mapping: {pw} takes exactly one of literal, column and ref_column")
        if "representation" in p and p["representation"] not in REPRESENTATIONS:
            _fail(f"mapping: {pw}.representation is one of {list(REPRESENTATIONS)}")
        if "lookup" in p and "ref_column" not in p:
            _fail(f"mapping: {pw}.lookup goes with ref_column")
        if p.get("unit_from_header") is not None:
            if p["unit_from_header"] is not True or "column" not in p or "unit" in p:
                _fail(f"mapping: {pw}.unit_from_header is true, with column and without unit")
        _censor(p.get("censor"), pw)
    results = [top_result] if top_result is not None else r.get("results") or []
    if not isinstance(results, list):
        _fail(f"mapping: {where}.results is a list")
    rw = "result" if top_result is not None else f"{where}.results"
    r["results"] = [_result(s, rw if top_result is not None else f"{rw}[{i}]", two) for i, s in enumerate(results)]
    links = top_links if top_links is not None else r.get("links") or []
    lw = "links" if top_links is not None else f"{where}.links"
    for i, ln in enumerate(links or []):
        _keys(ln, LINK_KEYS_2 if two else LINK_KEYS, f"{lw}[{i}]", ("type",))
        if sum(k in ln for k in ("target", "target_column", "target_record")) != 1:
            _fail(f"mapping: {lw}[{i}] takes exactly one of target and target_column"
                  + (" and target_record" if two else ""))
        if "target_lookup" in ln and "target_column" not in ln:
            _fail(f"mapping: {lw}[{i}].target_lookup goes with target_column")
        if "props" in ln and not isinstance(ln["props"], dict):
            _fail(f"mapping: {lw}[{i}].props is a mapping")
    r["links"] = list(links or [])
    r["_where"] = where
    return r


def _result(s: Any, where: str, two: bool) -> dict:
    required = ("variable",) if two else ("id_column", "column", "variable")
    _keys(s, RESULT_KEYS_2 if two else RESULT_KEYS, where, required)
    s = dict(s)
    for k in ("target", "target_column", "unresolved_target", "id", "id_column", "column", "basis_column",
              "unresolved_target_column"):
        _str(s, k, where)
    if sum(k in s for k in ("target", "target_column", "unresolved_target", "unresolved_target_column")) > 1:
        _fail(f"mapping: {where} takes at most one of target, target_column and unresolved_target"
              + (" and unresolved_target_column" if two else ""))
    if "produced_by" in s and "produced_by_column" in s:
        _fail(f"mapping: {where} takes produced_by or produced_by_column, not both")
    for k in ("phase", "stream", "basis"):
        if k in s and f"{k}_column" in s:
            _fail(f"mapping: {where} takes {k} or {k}_column, not both")
    if "target_lookup" in s and "target_column" not in s and "melt" not in s:
        _fail(f"mapping: {where}.target_lookup goes with target_column or melt")
    if s.get("unit_from_header") is not None and (s["unit_from_header"] is not True or "unit" in s):
        _fail(f"mapping: {where}.unit_from_header is true, without unit")
    if two:
        if ("melt" in s) == ("column" in s):
            _fail(f"mapping: {where} reads one column (column) or several (melt), exactly one")
        if "melt" not in s and not (s.get("id") or s.get("id_column")):
            _fail(f"mapping: {where} names its entries' ids: id, id_column or both")
        if s.get("id_slug") is not None and (s["id_slug"] is not True or not s.get("id_column")):
            _fail(f"mapping: {where}.id_slug is true, with id_column (its cells become snake_case ids)")
        if s.get("id") is not None and not RESULT_ID.fullmatch(s["id"]):
            _fail(f"mapping: {where}.id {s['id']!r} is not snake_case")
    if "melt" in s:
        if not isinstance(s["melt"], list) or not s["melt"]:
            _fail(f"mapping: {where}.melt is a list of the columns it reads, each {{column, id, target}}")
        if any(k in s for k in ("target", "target_column", "unresolved_target")):
            _fail(f"mapping: {where}: with melt, each column names its own target")
        ids, cols = set(), set()
        for j, c in enumerate(s["melt"]):
            mw = f"{where}.melt[{j}]"
            _keys(c, MELT_KEYS, mw, ("column", "id"))
            for k in MELT_KEYS:
                _str(c, k, mw)
            if not RESULT_ID.fullmatch(c["id"]):
                _fail(f"mapping: {mw}.id {c['id']!r} is not snake_case")
            if c["id"] in ids or c["column"] in cols:
                _fail(f"mapping: {mw} repeats an id or a column of the melt")
            if "target" in c and "unresolved_target" in c:
                _fail(f"mapping: {mw} takes target or unresolved_target, not both")
            if "unit" in c and s.get("unit_from_header"):
                _fail(f"mapping: {mw}.unit: the result reads its units from the headers (unit_from_header)")
            ids.add(c["id"])
            cols.add(c["column"])
    _missing(s.get("missing"), where, False)
    _censor(s.get("censor"), where)
    s["_where"] = where
    return s


def _references(refs: Any) -> None:
    if refs is None:
        return
    if not isinstance(refs, dict):
        _fail("mapping: references maps a handle to {id, label, props, reuse}")
    for h, x in refs.items():
        w = f"references.{h}"
        if not isinstance(h, str) or not HANDLE.fullmatch(h):
            _fail(f"mapping: {w}: a handle is snake_case")
        _keys(x, REFERENCE_KEYS, w, ("id", "label", "props"))
        _str(x, "id", w)
        _str(x, "label", w)
        if not isinstance(x["props"], dict) or not isinstance(x["props"].get("name"), str):
            _fail(f"mapping: {w}.props is a mapping with a name")
        if "reuse" in x and (not isinstance(x["reuse"], list) or not all(k in x["props"] for k in x["reuse"])):
            _fail(f"mapping: {w}.reuse lists properties the reference states (a record of its label with the same "
                  "values is used in its place)")


def _lookups(lk: Any) -> None:
    if lk is None:
        return
    if not isinstance(lk, dict):
        _fail("mapping: lookups maps a name to {label, values, aliases, match, create}")
    for name, x in lk.items():
        w = f"lookups.{name}"
        _keys(x, LOOKUP_KEYS, w, ("label",))
        for k in ("values", "aliases"):
            if k in x and (not isinstance(x[k], dict) or not all(isinstance(a, str) and isinstance(b, str)
                                                                  for a, b in x[k].items())):
                _fail(f"mapping: {w}.{k} maps a text to a text")
        if "match" in x and (not isinstance(x["match"], list) or not all(isinstance(p, str) for p in x["match"])):
            _fail(f"mapping: {w}.match lists the properties a cell's text is compared with (name, formula)")
        if "create" in x:
            c = _keys(x["create"], CREATE_KEYS, f"{w}.create", ("id_prefix", "as"))
            if c["as"] not in ("formula", "name"):
                _fail(f"mapping: {w}.create.as is formula (a ChemicalSpecies by its formula) or name")
            if "props" in c and not isinstance(c["props"], dict):
                _fail(f"mapping: {w}.create.props is a mapping")


# ------------------------------------------------------------------------------------------ compiling
def _seg(text: str) -> str:
    """One segment of a generated id: the cell's text, percent-encoded (``/`` and ``,`` included) so that two texts,
    and two keys joined by ``/`` or ``,``, never give one id."""
    return quote(text, safe="")


class _Compiler:
    def __init__(self, m: dict, header: list[str], body: list[list[str]], doc: dict, spec: dict, did: str):
        self.m, self.header, self.body, self.doc, self.spec = m, header, body, doc, spec
        self.d = m["dataset"]
        self.two = m["_two"]
        self.did = did
        self.by_key = self.d.get("row_selector", "key") == "key"
        self.pk = list(self.d.get("primary_key") or [])
        self.numeric: dict[str, dict] = {}          # column -> what it holds (unit, variable, basis), as numbers
        self.textual: set[str] = set()
        self.refs: dict[str, str] = {}              # an id the records name -> where
        self.tokens: dict[str, set] = {}            # column -> the missing tokens its mapping names
        self.prefixes: dict[str, tuple] = {}        # column -> the censoring prefixes its mapping names
        self.resolved: dict[tuple, str] = {}        # (column, cell text) -> the id a lookup gave it
        self.created: dict[str, dict] = {}          # id -> a reference record the import adds
        self.handles: dict[str, str] = {}           # a reference's handle -> its id
        self.reused: dict[str, str] = {}            # a reference's handle -> the existing record used in its place
        self.nodes_by_id = {n.get("id"): n for n in doc.get("nodes") or [] if isinstance(n, dict)}
        self.specs = {r["as"]: r for r in m["records"] if r.get("as")}
        synthetic = (doc.get("graph") or {}).get("data_status") == "synthetic_example"
        for r in m["records"]:
            if synthetic and r["origin"] != "synthetic":
                _fail(f"the destination is a synthetic example (data_status synthetic_example), whose records are all "
                      f"origin synthetic; the mapping says {r['origin']}")
            if not synthetic and r["origin"] == "synthetic":
                _fail("record.origin synthetic goes only into a synthetic example document")
        self.link_origin = "synthetic" if synthetic else "curated"
        self._resolve_references()

    def col(self, name: str, where: str) -> int:
        if name not in self.header:
            _fail(f"mapping: {where} names the column {name!r}, which the file's header does not have ({self.header})")
        return self.header.index(name)

    # ---- references and lookups (mapping_version 2)
    def _resolve_references(self) -> None:
        for h, x in sorted((self.m.get("references") or {}).items()):
            have = self.nodes_by_id.get(x["id"])
            if have is not None:
                if have.get("label") != x["label"]:
                    _fail(f"references.{h}: {x['id']} is in the document as a {have.get('label')}, not a {x['label']}")
                hp = have.get("props") or {}
                differ = sorted(k for k, v in x["props"].items() if k not in ("name", "description") and hp.get(k) != v)
                if differ:                          # one id, another meaning: refused, never reused
                    _fail(f"references.{h}: {x['id']} is in the document with other "
                          + ", ".join(f"{k} ({hp.get(k)!r}, the mapping {x['props'][k]!r})" for k in differ)
                          + "; give the reference another id")
                self.handles[h] = x["id"]
                continue
            if x.get("reuse"):
                hits = sorted(n["id"] for n in self.nodes_by_id.values() if n.get("label") == x["label"] and all(
                    (n.get("props") or {}).get(k) == x["props"][k] for k in x["reuse"]))
                if len(hits) > 1:
                    _fail(f"references.{h}: the document holds {len(hits)} {x['label']} records with "
                          + ", ".join(f"{k} {x['props'][k]!r}" for k in x["reuse"]) + f" ({', '.join(hits[:5])}); "
                          f"name the one to use as references.{h}.id")
                if hits:
                    self.handles[h] = hits[0]
                    self.reused[h] = hits[0]
                    continue
            self.handles[h] = x["id"]

    def ref(self, x: Any, where: str) -> Any:
        """An id as the mapping writes it: ``@handle`` names a reference."""
        if self.two and isinstance(x, str) and x.startswith("@"):
            h = x[1:]
            if h not in self.handles:
                _fail(f"mapping: {where} names the reference @{h}, which references does not declare")
            self._use_reference(h)
            return self.handles[h]
        return x

    def _use_reference(self, h: str) -> None:
        x = self.m["references"][h]
        rid = self.handles[h]
        if rid in self.nodes_by_id or rid in self.created:
            return
        self.created[rid] = {"id": rid, "label": x["label"],
                             "props": {**copy.deepcopy(x["props"]), "origin": self.link_origin}}

    def lookup(self, name: str, text: str, where: str, column: str | None) -> str:
        """The record id a lookup gives a cell's text (or a header's), creating the record when the lookup allows it."""
        lk = (self.m.get("lookups") or {}).get(name)
        if lk is None:
            _fail(f"mapping: {where} names the lookup {name!r}, which lookups does not declare")
        t = text.strip()
        if not t:
            _fail(f"{where}: a blank cell names no record (lookup {name})")
        t = (lk.get("aliases") or {}).get(t, t)
        rid = None
        if t in (lk.get("values") or {}):
            rid = self.ref(lk["values"][t], f"lookups.{name}.values")
        if rid is None:
            for prop in lk.get("match") or []:
                hits = sorted({n["id"] for n in [*self.nodes_by_id.values(), *self.created.values()]
                               if n.get("label") == lk["label"] and (n.get("props") or {}).get(prop) == t})
                if len(hits) > 1:
                    _fail(f"{where}: {text!r} names {len(hits)} {lk['label']} records by {prop} ({', '.join(hits[:5])});"
                          f" name the one under lookups.{name}.values")
                if hits:
                    rid = hits[0]
                    break
        if rid is None and lk.get("create"):
            rid = self._create(name, lk, t, where)
        if rid is None:
            _fail(f"{where}: {text!r} names no {lk['label']} of the document (lookup {name}): give it under "
                  f"lookups.{name}.values, add the record, or let the lookup create it")
        if column is not None:
            prev = self.resolved.setdefault((column, text), rid)
            if prev != rid:
                _fail(f"{where}: the text {text!r} of column {column!r} names {prev} and {rid}")
        return rid

    def _create(self, name: str, lk: dict, t: str, where: str) -> str:
        c = lk["create"]
        rid = c["id_prefix"] + _seg(t)
        if rid in self.nodes_by_id:
            have = self.nodes_by_id[rid]
            hp = have.get("props") or {}
            if have.get("label") != lk["label"] or hp.get(c["as"]) != t:
                _fail(f"{where}: lookup {name} would create {rid} for {t!r}, and the document holds that id as a "
                      f"{have.get('label')} whose {c['as']} is {hp.get(c['as'])!r}")
            return rid
        if rid in self.created:
            return rid
        props = {"name": t, **copy.deepcopy(c.get("props") or {})}
        if c["as"] == "formula":
            from .stoichiometry import parse_formula
            body = t.rstrip("+-")
            run = t[len(body):]
            if run and (len(set(run)) != 1 or body[-1:].isdigit()):
                _fail(f"{where}: {t!r} ends in a charge that can be read two ways; name its record under "
                      f"lookups.{name}.values")
            charge = (len(run) * (1 if run[0] == "+" else -1)) if run else 0
            comp, why = parse_formula(t, charge if run else None)
            if comp is None:
                _fail(f"{where}: {t!r} is no formula a {lk['label']} can be created from ({why}); name its record "
                      f"under lookups.{name}.values")
            props.update(formula=t, elemental_composition=comp, charge=charge)
        self.created[rid] = {"id": rid, "label": lk["label"], "props": {**props, "origin": self.link_origin}}
        return rid

    def unit_of(self, column: str, p: dict, where: str, kind: str | None) -> str:
        """The unit of a column: the mapping's, or (unit_from_header) the header's, which the quantity kind must read."""
        if not p.get("unit_from_header"):
            return p.get("unit")
        _, u = header_unit(column)
        if u is None:
            _fail(f"mapping: {where}: the header {column!r} writes no unit (as in 'FE CO (%)' or 'T / °C'); give unit")
        if kind:
            from .datatable import _kind_dims_error
            from .quantities import contracts
            c = contracts(self.spec)
            if c is not None and kind in c.kinds:
                reading = c.read_unit(u, tuple(x["context"] for x in c.kinds[kind]["compare_by"]))
                if not reading.read:
                    _fail(f"{where}: the unit {u!r} of the header {column!r} cannot be read as a {kind}; give unit")
                why = _kind_dims_error(self.spec, kind, u)
                if why:
                    _fail(f"{where}: the header {column!r}: {why}")
        return u

    def variable_kind(self, vid: str) -> str | None:
        n = self.nodes_by_id.get(vid) or self.created.get(vid)
        return ((n or {}).get("props") or {}).get("quantity_kind")

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

    def evidence(self, r: dict, i: int, row: list[str], column: str) -> tuple[str, dict]:
        text = row[self.col(column, "the mapping")]
        eid = f"{_seg(column) if self.two else column}.{self.row_tag(i, row)}"
        item = {"id": eid, "source": self.did, "kind": "dataset_row",
                "locator": f"{self.row_desc(i, row)}; column {column}", "dataset_cell": self.row_cell(i, row, column)}
        if text:
            item["quote"] = text
        if r.get("evidence_type"):
            item["evidence_type"] = r["evidence_type"]
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

    def prop_type(self, r: dict, prop: str) -> dict:
        from .quantities import prop_def
        d = prop_def(self.spec, {"label": r["label"], "props": {}}, prop)
        if not isinstance(d, dict):
            _fail(f"mapping: {'record' if not self.two else r['_where']}.properties.{prop} is not a property of "
                  f"{r['label']}")
        return d

    def prop_value(self, r: dict, prop: str, p: dict, i: int, row: list[str]) -> tuple[Any, tuple | None]:
        """(the value, (evidence id, item) or None) a row gives a property."""
        where = f"data row {i}, property {prop}"
        pw = f"{'record' if not self.two else r['_where']}.properties.{prop}"
        if "literal" in p:
            return copy.deepcopy(self.ref(p["literal"], pw)), None
        d = self.prop_type(r, prop)
        t = d.get("type")
        column = p.get("column") or p.get("ref_column")
        text = row[self.col(column, pw)]
        if "ref_column" in p:
            if t not in ("ref",):
                _fail(f"mapping: {pw}: ref_column goes with a reference, and {prop} is a {t}")
            v = self.value(text, p, where, raw=True, column=column)
            if v is not OMIT:
                if p.get("lookup"):
                    v = self.lookup(p["lookup"], v, where, column)
                self.refs.setdefault(v, f"{where} (column {column})")
                self.textual.add(column)
        elif t == "value":
            if p.get("unit") is None and p.get("representation") is None and not p.get("unit_from_header"):
                _fail(f"mapping: {pw} is a value: give its unit (a number) or its representation "
                      "(text_value, boolean_value)")
            if p.get("unit_from_header"):
                p = {**p, "unit": self.unit_of(column, p, pw, d.get("quantity"))}
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
            if p.get("unit") or p.get("representation") or p.get("unit_from_header"):
                _fail(f"mapping: {pw} is a {t}, not a value object: it takes no unit or representation")
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
            _fail(f"mapping: {pw} is a {t}: give it as a literal"
                  + (" or read it with ref_column" if t == "ref" else ""))
        if v is OMIT:
            return OMIT, None
        ev = self.evidence(r, i, row, column)
        if isinstance(v, dict) and "status" in v:
            v["evidence_ids"] = [ev[0]]
        return v, ev

    def results(self, r: dict, s: dict, i: int, row: list[str]) -> list[tuple[dict, list[tuple]]]:
        """The result entries one row gives by one result spec: one, or one per melted column."""
        if "melt" not in s:
            return [self.result(r, s, i, row, None)]
        return [self.result(r, s, i, row, c) for c in s["melt"]]

    def result(self, r: dict, s: dict, i: int, row: list[str], mc: dict | None) -> tuple[dict, list[tuple]]:
        where = f"data row {i}, result"
        sw = "result" if not self.two else s["_where"]
        parts = []
        if s.get("id_column"):
            cell = row[self.col(s["id_column"], f"{sw}.id_column")]
            parts.append(_snake(cell) if s.get("id_slug") else cell)
        if mc is not None:
            parts.append(mc["id"])
        elif s.get("id"):
            parts.append(s["id"])
        rid = "_".join(parts)
        if not RESULT_ID.fullmatch(rid):
            _fail(f"{where}: the result id {rid!r}" + (f" (column {s['id_column']})" if s.get("id_column") else "")
                  + " is not snake_case (lower-case letters, digits and _, starting with a letter)")
        column = mc["column"] if mc is not None else s["column"]
        var = self.ref(s["variable"], f"{sw}.variable")
        if s.get("unit_from_header"):
            unit = self.unit_of(column, s, f"{sw} (column {column!r})", self.variable_kind(var))
        else:
            unit = (mc or {}).get("unit") or s.get("unit")
        if unit is None:
            _fail(f"mapping: {sw}.unit is the unit of the result column")
        v = self.value(row[self.col(column, f'{sw}.column' if mc is None else f'{sw}.melt')], {**s, "unit": unit},
                       where, raw=False, column=column)
        eid, item = self.evidence(r, i, row, column)
        out = {"id": rid, **v, "variable": var, "evidence_ids": [eid]}
        self.refs.setdefault(var, f"{sw}.variable")
        evs = [(eid, item)]
        if mc is not None:
            if mc.get("target"):
                tgt = self.lookup(s["target_lookup"], mc["target"], f"{sw}.melt ({column})", None) \
                    if s.get("target_lookup") else self.ref(mc["target"], f"{sw}.melt")
                out["target"] = tgt
                self.refs.setdefault(tgt, f"{sw}.melt ({column})")
            elif mc.get("unresolved_target"):
                out["unresolved_target"] = mc["unresolved_target"]
        elif s.get("target"):
            out["target"] = self.ref(s["target"], f"{sw}.target")
            self.refs.setdefault(out["target"], "result.target")
        elif s.get("target_column"):
            t = row[self.col(s["target_column"], f"{sw}.target_column")]
            if not t:
                _fail(f"{where}: the product (column {s['target_column']}) is blank")
            if s.get("target_lookup"):
                t = self.lookup(s["target_lookup"], t, where, s["target_column"])
            out["target"] = t
            self.refs.setdefault(t, f"{where} (column {s['target_column']})")
            evs.append(self.evidence(r, i, row, s["target_column"]))
            self.textual.add(s["target_column"])
        elif s.get("unresolved_target"):
            out["unresolved_target"] = s["unresolved_target"]
        elif s.get("unresolved_target_column"):
            t = row[self.col(s["unresolved_target_column"], f"{sw}.unresolved_target_column")]
            if t:                                   # what the cell names, which no record of the document identifies
                out["unresolved_target"] = t
                evs.append(self.evidence(r, i, row, s["unresolved_target_column"]))
                self.textual.add(s["unresolved_target_column"])
        prod = s.get("produced_by")
        if prod and prod != "self":
            out["produced_by"] = self.ref(prod, f"{sw}.produced_by")
            self.refs.setdefault(out["produced_by"], "result.produced_by")
        elif s.get("produced_by_column"):
            t = row[self.col(s["produced_by_column"], f"{sw}.produced_by_column")]
            if not t:
                _fail(f"{where}: the producer (column {s['produced_by_column']}) is blank")
            out["produced_by"] = t
            self.refs.setdefault(t, f"{where} (column {s['produced_by_column']})")
            evs.append(self.evidence(r, i, row, s["produced_by_column"]))
            self.textual.add(s["produced_by_column"])
        for k in ("phase", "stream", "basis"):
            fixed = (mc or {}).get(k) or s.get(k)
            if fixed:
                out[k] = fixed
            elif s.get(f"{k}_column"):
                t = row[self.col(s[f"{k}_column"], f"{sw}.{k}_column")]
                if t:
                    out[k] = t
                    evs.append(self.evidence(r, i, row, s[f"{k}_column"]))
                    self.textual.add(s[f"{k}_column"])
        for k in ("definition", "origin"):
            if (mc or {}).get(k) or s.get(k):
                out[k] = (mc or {}).get(k) or s[k]
        self.numeric[column] = {"unit": unit, "variable": var, **({"basis": s["basis"]} if s.get("basis") else {})}
        out["evidence_ids"] = [e[0] for e in evs]      # the cells that state it: its number and what identifies it
        return out, evs

    def record_id(self, r: dict, key: tuple) -> str:
        if r.get("id_prefix"):
            prefix = r["id_prefix"]
        else:
            prefix = f"{self.did}/{(r.get('as') if self.two else None) or r['label'].lower()}/"
        return prefix + "/".join(_seg(v) for v in key)

    def compile(self) -> tuple[dict, list[dict], list[dict]]:
        rows = self.rows()
        nodes, links = [], []
        for r in self.m["records"]:
            n, ln = self.compile_record(r, rows)
            nodes += n
            links += ln
        return self.dataset(), nodes, links

    def compile_record(self, r: dict, rows: list) -> tuple[list[dict], list[dict]]:
        rw = "record" if not self.two else r["_where"]
        kpos = [self.col(c, f"{rw}.key") for c in r["key"]]
        groups: dict[tuple, list[tuple[int, list[str]]]] = {}
        for i, row in rows:
            key = tuple(row[p] for p in kpos)
            blank = [c for c, v in zip(r["key"], key) if v == ""]
            if blank:
                _fail(f"data row {i}: the record key column {blank[0]!r} is blank")
            groups.setdefault(key, []).append((i, row))
        nodes, links = [], []
        for key, members in sorted(groups.items()):
            rid = self.record_id(r, key)
            props: dict = {"origin": r["origin"]}
            evidence: dict[str, dict] = {}
            # the record's name and recording time: one per record
            for what, col in (("name", r.get("name_column")), ("recorded_at", r.get("recorded_at_column"))):
                if col:
                    got = {row[self.col(col, f'{rw}.{what}_column')] for _, row in members}
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
                got = [(i, *self.prop_value(r, prop, p, i, row)) for i, row in members]
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
            if r["results"]:
                results = []
                for i, row in members:
                    for s in r["results"]:
                        for entry, evs in self.results(r, s, i, row):
                            if any(x["id"] == entry["id"] for x in results):
                                where = s.get("id_column") or "id"
                                _fail(f"duplicate key: {rid} has two results {entry['id']!r} (column {where})"
                                      if s.get("id_column") or not self.two else
                                      f"duplicate key: {rid} has two results {entry['id']!r}: its rows need an "
                                      f"id_column ({s['_where']})")
                            results.append(entry)
                            for ev in evs:
                                _put(evidence, *ev)
                props["result_values"] = sorted(results, key=lambda x: x["id"])
            if not evidence and self.two:
                if not r.get("name_column"):
                    _fail(f"mapping: {rw}: {rid} states nothing the table holds (no property or result read from a "
                          "column); give it a name_column, whose cell then backs its REPORTED_IN link")
                _put(evidence, *self.evidence(r, *members[0], r["name_column"]))
            props["evidence"] = [evidence[k] for k in sorted(evidence)]
            nodes.append({"id": rid, "label": r["label"], "props": props})
            for j, ln in enumerate(r["links"]):
                lw = f"links[{j}]" if not self.two else f"{r['_where']}.links[{j}]"
                ev = None
                if "target" in ln:
                    tgt = self.ref(ln["target"], lw)
                elif "target_record" in ln:
                    other = self.specs[ln["target_record"]]
                    pos = [self.col(c, f"{lw}.target_record") for c in other["key"]]
                    got = {tuple(row[p] for p in pos) for _, row in members}
                    if len(got) > 1:
                        _fail(f"conflicting run-level property {lw} ({ln['type']}) for {rid}: its rows name "
                              f"{len(got)} {other['label']} records ({', '.join('/'.join(k) for k in sorted(got)[:4])})")
                    k = got.pop()
                    if any(v == "" for v in k):
                        _fail(f"{rid}: {lw} ({ln['type']}): the key of the {other['label']} is blank")
                    tgt = self.record_id(other, k)
                else:
                    got = {row[self.col(ln["target_column"], f"{lw}.target_column")] for _, row in members}
                    if len(got) > 1:
                        _fail(f"conflicting run-level property {lw} ({ln['type']}) for {rid}: column "
                              f"{ln['target_column']} holds {sorted(got)}")
                    tgt = got.pop()
                    if not tgt:
                        _fail(f"{rid}: {lw} ({ln['type']}): the target (column {ln['target_column']}) is blank")
                    if ln.get("target_lookup"):
                        tgt = self.lookup(ln["target_lookup"], tgt, f"{rid}: {lw}", ln["target_column"])
                    ev = self.evidence(r, *members[0], ln["target_column"])
                    self.textual.add(ln["target_column"])
                self.refs.setdefault(tgt, lw)
                lp = {"origin": self.link_origin, **copy.deepcopy(ln.get("props") or {})}
                if ev:
                    lp["evidence"] = [ev[1]]
                links.append({"id": f"{rid}/{ln['type'].lower().replace('_', '-')}/{_seg(tgt)}", "source": rid,
                              "target": tgt, "type": ln["type"], "props": lp})
            rows_cited = sorted({e["locator"].split(";")[0] for e in evidence.values()})
            links.append({"id": f"{rid}/reported-in/{self.did.rsplit('/', 1)[-1]}", "source": rid, "target": self.did,
                          "type": "REPORTED_IN", "props": {"origin": self.link_origin, "locators": rows_cited}})
        return nodes, links

    def dataset(self) -> dict:
        over = self.d.get("columns") or {}
        unknown = sorted(set(over) - set(self.header))
        if unknown:
            _fail(f"mapping: dataset.columns names {unknown}, which the file's header does not have")
        dt = {c: "datetime" for r in self.m["records"] for c in [r.get("recorded_at_column")] if c}
        cols = []
        for name in self.header:
            c = {"name": name, "datatype": dt.get(name, "number" if name in self.numeric else "string"),
                 **self.numeric.get(name, {})}
            c.update(over.get(name) or {})
            if c.get("variable"):
                c["variable"] = self.ref(c["variable"], f"dataset.columns.{name}.variable")
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


def _snake(text: str) -> str:
    """A cell's text as a snake_case id segment (id_slug): lower case, every other character run as _, a leading digit
    behind r_. Two texts that give one id are refused as a duplicate result, never merged."""
    t = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return t if re.match(r"^[a-z]", t) else f"r_{t}" if t else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "table"


# ------------------------------------------------------------------------------------------ the import
def plan(data: bytes, mapping: bytes, doc: dict, spec: dict, file_name: str = "the file", uri: str | None = None) -> dict:
    """What importing ``data`` (a file's bytes) by ``mapping`` (YAML bytes) into ``doc`` (validated under ``spec``)
    adds: ``{document, added, unchanged, dataset, records, results, validation, warnings}``. Refuses (ScaleError) a
    malformed mapping or file, duplicate keys, conflicting run-level properties, unresolved references, malformed numbers,
    a conflict with what the document holds and any validation error the import would introduce. ``uri`` is the file's
    location relative to the document, used by a version 2 mapping that names neither a version nor a uri."""
    from . import pgraph
    if not isinstance(spec.get("dataset_cell"), dict):
        _fail(f"the destination pins scale-catal {spec.get('version')}, which has no dataset cells (0.8.4 on); "
              "migrate it first (scale graph migrate)")
    m = load_mapping(mapping)
    d = m["dataset"]
    file_digest = sha256(data)
    extracted = None
    if m["_two"] and "input" in m:
        from .datainput import extract
        extracted = extract(data, m["input"], file_name)
        header, body = extracted["header"], extracted["body"]
        table_bytes = extracted["canonical"]
    else:
        header, body = read_table(data, d["profile"], file_name)
        table_bytes = data
    if m["_two"] and d["profile"] != "scale_csv_v1":
        _fail(f"mapping: dataset.profile {d['profile']!r}: the table a version 2 mapping reads is scale_csv_v1 (another "
              "dialect or a workbook is read by its input block)")
    base = f"{d.get('id_prefix')}{_slug(Path(file_name).stem)}-{file_digest[:12]}"
    if d.get("id"):
        did = d["id"]
    elif extracted is not None and not extracted["identity"]:
        did = f"{base}/table-{sha256(table_bytes)[:12]}"   # one table of the file (a sheet, a range): its own id
    else:
        did = base
    if m["_two"] and not (d.get("version") or d.get("uri")):
        d = m["dataset"] = {**d, "uri": uri or file_name}
    comp = _Compiler(m, header, body, doc, spec, did)
    table, nodes, links = comp.compile()
    have = {n.get("id"): n for n in doc.get("nodes") or []} | {e.get("id"): e for e in doc.get("links") or []}
    derived = extracted is not None and not extracted["identity"]
    fid = (d.get("file_id") or (base if not d.get("id") else f"{did}/file")) if derived else None
    generated = {n["id"] for n in nodes} | {did} | set(comp.created) | ({fid} if fid else set())
    unresolved = sorted((x, w) for x, w in comp.refs.items() if x not in have and x not in generated)
    if unresolved:
        _fail("unresolved references (the destination must hold what the rows name): "
              + "; ".join(f"{x} ({w})" for x, w in unresolved[:20]))
    digest = sha256(table_bytes)
    old = have.get(did)
    if old is not None and (old.get("label") != "Dataset" or (old.get("props") or {}).get("sha256") != digest):
        _fail(f"{did} is already in the document with other bytes (sha256 "
              f"{(old.get('props') or {}).get('sha256')}); new bytes are a new Dataset: give it a new id and version, "
              "and link it to this one with UPDATES")
    table["ingest"] = {"mapping_sha256": sha256(mapping), "tool": f"scale-catal {__version__}",
                       "schema_version": str(spec.get("version")), "schema_sha256": pgraph.spec_sha256(spec)}
    if extracted is not None:
        from .datainput import recipe_strings
        table["ingest"].update(recipe_strings(extracted["recipe"]))
        table["ingest"]["input_rows"] = extracted["rows_used"]
        if derived:
            table["ingest"]["extracted_from"] = fid
    for n, ((column, text), rid) in enumerate(sorted(comp.resolved.items())):
        table["ingest"][f"lookup_{n:04d}"] = json.dumps([column, text, rid], ensure_ascii=False)
    synthetic = m["records"][0]["origin"] == "synthetic"
    origin = "synthetic" if synthetic else "curated"
    meta = {k: d[k] for k in ("licence", "description") if d.get(k)}
    if derived:
        rec = extracted["recipe"]
        where = (f"sheet {rec['sheet']!r}, " if rec.get("sheet") else "") + f"cells {extracted['rows_used']}"
        ds = {"id": did, "label": "Dataset", "props": {
            "name": f"{d.get('name') or file_name} ({where}, as scale_csv_v1)", "data_kind": "table",
            "availability": d.get("availability", "available"), "sha256": digest, "media_type": "text/csv",
            "table": table, "origin": "synthetic" if synthetic else "derived", "version": d.get("version") or "1",
            "description": f"The table of {where} of {file_name} (sha256 {file_digest}), header row "
                           f"{rec['header_row']}, written as scale_csv_v1 by scale data add; the recipe is in "
                           "table.ingest (input_*), so scale data verify extracts these bytes again from the file.",
            **({"licence": d["licence"]} if d.get("licence") else {})}}
        fds = {"id": fid, "label": "Dataset", "props": {
            "name": d.get("name") or file_name, "data_kind": "table", "availability": d.get("availability", "available"),
            "sha256": file_digest, "media_type": extracted["media_type"], "origin": origin,
            **{k: d[k] for k in ("version", "uri") if d.get(k)}, **meta}}
        new = [fds, ds]
        links = links + [{"id": f"{fid}/includes/{did.rsplit('/', 1)[-1]}", "source": fid, "target": did,
                          "type": "INCLUDES", "props": {"origin": origin}}]
    else:
        ds = {"id": did, "label": "Dataset", "props": {
            "name": d.get("name") or file_name, "data_kind": "table", "availability": d.get("availability", "available"),
            "sha256": digest, "media_type": "text/csv", "table": table, "origin": origin,
            **{k: d[k] for k in ("version", "uri", "licence", "description") if d.get(k)}}}
        new = [ds]
    new += [comp.created[k] for k in sorted(comp.created)]
    new += nodes
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
    ours = {n["id"] for n in nodes} | {did}
    conf = {}
    for ref, c in ((after.conformance or {}).get("profiles") or {}).items():
        mine = [v for v in c.get("violations") or [] if v.get("record") in ours]
        conf[ref] = {"violations": len(mine), "first": mine[:10]}
    res = {"document": out, "added": [x["id"] for x in added], "unchanged": unchanged, "dataset": did,
           "sha256": digest, "records": len(nodes), "results": sum(len(n["props"].get("result_values") or [])
                                                                  for n in nodes),
           "validation": {"valid": after.ok, "errors": len(after.errors), "new_warnings": len(warnings)},
           "warnings": warnings[:50], "checklist": conf, "nodes": [x for x in added if "label" in x],
           "links": [x for x in added if "label" not in x]}
    if m["_two"]:
        res["created"] = sorted(comp.created)
        res["reused"] = dict(sorted(comp.reused.items()))
        if extracted is not None:
            res["input"] = {**extracted["recipe"], "cells": extracted["rows_used"],
                            "rows_ignored_after_a_blank_row": extracted["ignored_after"],
                            **({"file_dataset": fid, "file_sha256": file_digest} if derived else {})}
    return res


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
    """Import the file ``csv_path`` (a CSV file, or a workbook a version 2 mapping reads) by ``map_path`` into the
    document ``into`` (a JSON file), atomically; with ``dry_run`` nothing is written. Returns the plan without the whole
    document."""
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
    try:
        uri = Path(os.path.relpath(Path(csv_path).resolve(), into.resolve().parent)).as_posix()
    except ValueError:                              # another drive (Windows): the name only
        uri = Path(csv_path).name
    res = plan(data, mapping, doc, spec, Path(csv_path).name, uri=uri)
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
