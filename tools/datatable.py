"""Tabular Datasets and the cells evidence names in them (scale-catal 0.8.4 on).

A value reaches its data by one route, whatever produced it: ``value.evidence_ids`` -> an evidence item with a
``dataset_cell`` (a column and a row, by key or position) -> ``evidence.source``, a Dataset whose ``table`` says how
its bytes are read (a profile), its primary key and its columns (datatype, the Variable a column holds values of, its
unit and basis). The data stay outside the graph; the Dataset names them by version or uri and by the sha256 of their
exact bytes.

This module

* reads a file under a table profile (:func:`read_table`; ``scale_csv_v1``: UTF-8, comma, double-quote escaping, one
  header row, nothing skipped; a header naming a column twice, a row of another width, an empty row are refused);
* checks a document's tables and cells (:func:`check_document`, run by the validator): a cell sits on ``dataset_row``
  evidence of a Dataset with a table, a version or uri and a sha256; it names its row by exactly one of a key (every
  primary-key column once) and a position; its column is the table's; a numeric value citing a numeric column is in
  the column's unit, of the column's variable and basis where both state one; a column a result entry cites, or one
  holding values of a variable, names a quantitative Variable with a quantity_kind and a unit of its kind's
  dimensions. Nothing is converted;
* verifies a document against local copies of its data (:func:`verify`): the file's sha256 is the one recorded, and
  each cited cell holds what the record says. A dataset whose file is not at hand is ``unverified``, never verified;
  no URL is fetched.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

from .util import ScaleError

PROFILES = ("scale_csv_v1",)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NUMERIC = ("number", "integer")
# a number as a cell of scale_csv_v1 writes it: no spaces, no thousands separators, no NaN or infinity
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


# ------------------------------------------------------------------------------------------ reading
def read_table(data: bytes, profile: str = "scale_csv_v1", where: str = "the file") -> tuple[list[str], list[list[str]]]:
    """(the header, the data rows) of ``data`` read under ``profile``. Refuses what the profile does not allow, saying
    where."""
    if profile not in PROFILES:
        raise ScaleError(f"{where}: unknown table profile {profile!r} (known: {', '.join(PROFILES)})")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ScaleError(f"{where}: starts with a byte-order mark; scale_csv_v1 is UTF-8 without one")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScaleError(f"{where}: not UTF-8 ({exc})") from exc
    rows: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=",", quotechar='"', doublequote=True, strict=True):
            rows.append(row)
    except csv.Error as exc:
        raise ScaleError(f"{where}: not readable as scale_csv_v1 ({exc})") from exc
    if not rows:
        raise ScaleError(f"{where}: empty; scale_csv_v1 has one header row")
    header, body = rows[0], rows[1:]
    if any(not h for h in header):
        raise ScaleError(f"{where}: the header has an empty column name")
    dup = sorted({h for h in header if header.count(h) > 1})
    if dup:
        raise ScaleError(f"{where}: the header names {', '.join(map(repr, dup))} more than once")
    for i, row in enumerate(body):
        if not row:
            raise ScaleError(f"{where}: data row {i} is empty (scale_csv_v1 skips no rows)")
        if len(row) != len(header):
            raise ScaleError(f"{where}: data row {i} has {len(row)} fields, the header {len(header)}")
    return header, body


def parse_number(text: str) -> int | float | None:
    """The number a cell writes (an int when it has no point or exponent), or None when it is not one."""
    if not isinstance(text, str) or not NUMBER.fullmatch(text):
        return None
    return int(text) if re.fullmatch(r"[+-]?\d+", text) else float(text)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------------------------ validation
def _dataset_labels(spec: dict | None) -> set[str]:
    """Dataset and the labels that are one (a TimeSeries), as the schema says; without one, the kernel's and the
    catalysis domain's."""
    if not spec:
        return {"Dataset", "TimeSeries"}
    return {lab for lab, d in (spec.get("nodes") or {}).items()
            if lab == "Dataset" or "Dataset" in (d.get("ancestors") or [])}


def _tables(doc: dict, spec: dict | None = None) -> dict[str, dict]:
    """Dataset id -> its props, for the Datasets of a document (a label that is_a Dataset included)."""
    labels = _dataset_labels(spec)
    return {n["id"]: n.get("props") or {} for n in doc.get("nodes") or []
            if isinstance(n, dict) and n.get("label") in labels and isinstance(n.get("id"), str)}


def _list(x: Any) -> list:
    """A list as it is, anything else (malformed, which the type check reports) as none."""
    return x if isinstance(x, list) else []


def _columns(table: Any) -> dict[str, dict]:
    cols = table.get("columns") if isinstance(table, dict) and isinstance(table.get("columns"), list) else None
    return {c["name"]: c for c in cols or [] if isinstance(c, dict) and isinstance(c.get("name"), str)}


def _kind_dims_error(spec: dict, kind: Any, unit: Any) -> str | None:
    """Why ``unit`` is not of a dimension of quantity kind ``kind`` under the schema's contracts, or None (also when
    the kind is not one the contracts declare or the unit cannot be read: those are not checked)."""
    from .quantities import contracts
    c = contracts(spec)
    if c is None or not isinstance(kind, str) or kind not in c.kinds or not isinstance(unit, str) or not unit.strip():
        return None
    reading = c.read_unit(unit, tuple(x["context"] for x in c.kinds[kind]["compare_by"]))
    if reading.read and reading.vector not in c.kinds[kind]["dimensions"]:
        dims = ", ".join(c.dimension_name(x) for x in c.kinds[kind]["dimensions"])
        return f"unit {unit!r} is {c.dimension_name(reading.vector)}, not a dimension of {kind} ({dims})"
    return None


def check_document(doc: dict, spec: dict, rep) -> None:
    """The rules of the kernel's dataset_cell, dataset_table and dataset_column (errors; see the module docstring)."""
    datasets = _tables(doc, spec)
    labels = {n.get("id"): n.get("label") for n in doc.get("nodes") or [] if isinstance(n, dict)}
    nodes = {n.get("id"): n.get("props") or {} for n in doc.get("nodes") or [] if isinstance(n, dict)}
    for did, p in datasets.items():
        t = p.get("table")
        if not isinstance(t, dict):
            continue
        names = [c.get("name") for c in _list(t.get("columns")) if isinstance(c, dict)]
        for n in sorted({x for x in names if isinstance(x, str) and names.count(x) > 1}):
            rep.add("error", did, f"table: column {n!r} is named more than once")
        pk = t.get("primary_key") if isinstance(t.get("primary_key"), list) else []
        for k in sorted({x for x in pk if isinstance(x, str) and pk.count(x) > 1}):
            rep.add("error", did, f"table: primary_key names {k!r} more than once")
        for k in pk:
            if isinstance(k, str) and k not in names:
                rep.add("error", did, f"table: primary_key column {k!r} is not a column of the table")
        if isinstance(t.get("rows"), int) and not isinstance(t.get("rows"), bool) and t["rows"] < 0:
            rep.add("error", did, "table: rows is at least 0")
        for c in _list(t.get("columns")):
            if not isinstance(c, dict) or c.get("datatype") not in NUMERIC or not isinstance(c.get("variable"), str):
                continue
            var = nodes.get(c["variable"]) if labels.get(c["variable"]) == "Variable" else None
            if var is None:
                continue                                    # an unresolved reference is reported by the type check
            where = f"table: column {c.get('name')!r}"
            if var.get("scale") != "quantitative" or not var.get("quantity_kind"):
                rep.add("error", did, f"{where} holds numbers of {c['variable']}, which is not a quantitative Variable "
                                      "with a quantity_kind")
            if not isinstance(c.get("unit"), str) or not c["unit"].strip():
                rep.add("error", did, f"{where} holds numbers of {c['variable']} and states no unit")
            else:
                why = _kind_dims_error(spec, var.get("quantity_kind"), c["unit"])
                if why:
                    rep.add("error", did, f"{where}: {why}: the {var.get('quantity_kind')} contract")
    from .quantities import _values_anywhere
    for r in [*(doc.get("nodes") or []), *(doc.get("links") or [])]:
        if not isinstance(r, dict):
            continue
        rid, props = str(r.get("id")), r.get("props") if isinstance(r.get("props"), dict) else {}
        ev = props.get("evidence") if isinstance(props.get("evidence"), list) else []
        cells: dict[str, tuple[dict, dict]] = {}           # evidence id -> (its cell, its dataset's column)
        for i, e in enumerate(ev):
            if not isinstance(e, dict) or not isinstance(e.get("dataset_cell"), dict):
                continue
            cell, where = e["dataset_cell"], f"evidence[{i}].dataset_cell"
            if e.get("kind") != "dataset_row":
                rep.add("error", rid, f"{where} goes on evidence of kind dataset_row, not {e.get('kind')!r}")
            src = e.get("source") if isinstance(e.get("source"), str) else None
            if src not in datasets:
                if src in labels:
                    rep.add("error", rid, f"{where} names a cell of {src}, a {labels.get(src)}; a cell is one of a Dataset")
                continue
            d = datasets[src]
            t = d.get("table")
            if not isinstance(t, dict):
                rep.add("error", rid, f"{where} names a cell of {src}, which describes no table (Dataset.table)")
                continue
            if not any(isinstance(d.get(k), str) and d[k].strip() for k in ("version", "uri")):
                rep.add("error", rid, f"{where} names a cell of {src}, which states neither its version nor its uri")
            if not (isinstance(d.get("sha256"), str) and SHA256.match(d["sha256"])):
                rep.add("error", rid, f"{where} names a cell of {src}, whose sha256 is not 64 hexadecimal characters")
            has_key, has_index = "row_key" in cell, "row_index" in cell
            if has_key == has_index:
                rep.add("error", rid, f"{where} names its row by exactly one of row_key and row_index")
            pk = [x for x in _list(t.get("primary_key")) if isinstance(x, str)]
            if has_key and isinstance(cell.get("row_key"), list):
                cols = [k.get("column") for k in cell["row_key"] if isinstance(k, dict)]
                if not pk:
                    rep.add("error", rid, f"{where}: row_key names a row of {src}, whose table has no primary key")
                elif sorted(cols, key=str) != sorted(pk) or len(set(cols)) != len(cols):
                    rep.add("error", rid, f"{where}: row_key names {cols}, not every primary-key column of {src} once "
                                          f"({pk})")
            if has_index and isinstance(cell.get("row_index"), int) and not isinstance(cell.get("row_index"), bool):
                n = t.get("rows")
                if cell["row_index"] < 0 or (isinstance(n, int) and not isinstance(n, bool) and cell["row_index"] >= n):
                    rep.add("error", rid, f"{where}: row_index {cell['row_index']} is not a data row of {src}"
                                          + (f" (0 to {n - 1})" if isinstance(n, int) and n > 0 else ""))
            col = _columns(t).get(cell["column"]) if isinstance(cell.get("column"), str) else None
            if col is None:
                if isinstance(cell.get("column"), str):
                    rep.add("error", rid, f"{where}: column {cell['column']!r} is not a column of {src}")
                continue
            if isinstance(e.get("id"), str):
                cells[e["id"]] = (cell, {**col, "_dataset": src})
        if not cells:
            continue
        for path, v in _values_anywhere(props):
            for eid in v.get("evidence_ids") or [] if isinstance(v.get("evidence_ids"), list) else []:
                if eid not in cells:
                    continue
                cell, col = cells[eid]
                _check_value_cell(rep, rid, f"props{path}", v, col, path.startswith("/result_values/"), nodes, labels)


def _check_value_cell(rep, rid: str, where: str, v: dict, col: dict, result: bool, nodes: dict, labels: dict) -> None:
    """A value against the column of the cell it cites: its unit, variable and basis; a result's column names its
    Variable."""
    from .quantities import numbered
    name, src = col.get("name"), col["_dataset"]
    at = f"{where}: the cell it cites ({src}, column {name!r})"
    numeric = col.get("datatype") in NUMERIC
    cvar = col.get("variable") if isinstance(col.get("variable"), str) else None
    if numeric and (result or isinstance(v.get("variable"), str)) and cvar is None:
        rep.add("error", rid, f"{at} is a numeric column that names no Variable; a column a result or a variable's "
                              "value cites names a quantitative Variable and its unit")
    if cvar is not None and isinstance(v.get("variable"), str) and v["variable"] != cvar:
        rep.add("error", rid, f"{at} holds values of {cvar}, not of {v['variable']}")
    if numeric and numbered(v) or (numeric and v.get("status") in ("below_detection_limit", "below_quantification_limit")):
        cu = col.get("unit")
        if not isinstance(cu, str) or not cu.strip():
            rep.add("error", rid, f"{at} is a numeric column that states no unit")
        elif v.get("unit") != cu:
            rep.add("error", rid, f"{at} is in {cu!r}, the value in {v.get('unit')!r}; nothing is converted")
    if isinstance(col.get("basis"), str) and isinstance(v.get("basis"), str) and v["basis"] != col["basis"]:
        rep.add("error", rid, f"{at} states the basis {col['basis']!r}, the value {v['basis']!r}")


# ------------------------------------------------------------------------------------------ verification
def _row_of(cell: dict, header: list[str], body: list[list[str]], index: dict) -> tuple[list[str] | None, str | None]:
    """The row a cell names, or why none: (row, None) or (None, reason)."""
    if "row_index" in cell:
        i = cell.get("row_index")
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(body):
            return body[i], None
        return None, f"row_index {i} is not a data row of the file ({len(body)} rows)"
    key = cell.get("row_key")
    if not isinstance(key, list) or not all(isinstance(k, dict) for k in key):
        return None, "names no row"
    cols = tuple(sorted(str(k.get("column")) for k in key))
    if any(c not in header for c in cols):
        return None, f"row_key names a column the file does not have ({', '.join(c for c in cols if c not in header)})"
    if cols not in index:
        pos = [header.index(c) for c in cols]
        idx: dict[tuple, list[int]] = {}
        for i, row in enumerate(body):
            idx.setdefault(tuple(row[p] for p in pos), []).append(i)
        index[cols] = idx
    want = tuple(str(k.get("value")) for k in sorted(key, key=lambda k: str(k.get("column"))))
    hits = index[cols].get(want, [])
    if len(hits) != 1:
        return None, (f"row_key {dict(zip(cols, want))} names {'no' if not hits else len(hits)} rows of the file"
                      + ("" if not hits else "; a key names one row"))
    return body[hits[0]], None


# the fields of a value that say what it is a value of, or its text (a cell of a text column it cites holds one)
QUALIFIERS = ("target", "unresolved_target", "produced_by", "calibration", "phase", "stream", "variable", "id",
              "text_value", "ref_value", "basis", "definition", "as_reported")
TRAILING_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _number_in(text: str) -> int | float | None:
    """The number a cell writes, or the number that ends a token (``<0.5``)."""
    got = parse_number(text)
    if got is None:
        m = TRAILING_NUMBER.search(text)
        got = parse_number(m.group(0)) if m else None
    return got


MISSING = ("not_reported", "not_measured", "not_applicable")
CENSORED = ("below_detection_limit", "below_quantification_limit")


def _same_number(value: int | float, text: str) -> bool:
    """Whether the number a cell writes (alone, or at the end of a token) is the value's, exactly: the decimal the
    cell writes against the decimal the value is (an integer is never rounded through a float)."""
    from decimal import Decimal, InvalidOperation
    m = TRAILING_NUMBER.search(text)
    try:
        return m is not None and Decimal(m.group(0)) == Decimal(repr(value) if isinstance(value, float) else value)
    except InvalidOperation:
        return False


def _holds(text: str, v: dict, numeric: bool, named: dict | None = None) -> tuple[bool | None, str]:
    """Whether a cell's text holds what value ``v`` states (True, False, or None when it cannot be compared), and what
    was compared. A cell of a numeric column holds the value's number exactly; a token (``<0.5``) holds only a censored
    value (a limit or a one-sided bound) that records it as reported and whose number it ends with, never an exact one;
    a blank or a recorded token holds a value missing; a cell of another column holds one of the value's qualifiers
    (its product, producer, phase, stream) or its text."""
    st = v.get("status")
    told = v.get("as_reported")
    if not numeric:
        fields = {k: v[k] for k in QUALIFIERS if isinstance(v.get(k), str)}
        if isinstance(v.get("boolean_value"), bool):
            fields["boolean_value"] = str(v["boolean_value"]).lower()
        hit = [k for k, x in fields.items() if x == (text.lower() if k == "boolean_value" else text)]
        if hit:
            return True, f"cell {text!r} is its {hit[0]}"
        hit = [k for k, x in fields.items() if (named or {}).get(text) == x]
        if hit:
            return True, f"cell {text!r} names its {hit[0]} {fields[hit[0]]} (the table's lookup)"
        if st in MISSING and (text == "" or text == told):
            return True, f"cell {text!r} for a value {st}"
        return False, f"cell {text!r} is none of its {sorted(fields)}"
    if st in MISSING:
        return text == "" or (isinstance(told, str) and text == told), f"cell {text!r} for a value {st}"
    if isinstance(told, str) and text != told:
        return False, f"cell {text!r}, recorded as reported {told!r}"
    if not (isinstance(v.get("value"), (int, float)) and not isinstance(v.get("value"), bool)):
        return None, "the value states no single number (a range) to compare with a numeric cell"
    if parse_number(text) is None:                      # a token: only a censored value may hold it
        if _number_in(text) is None:
            return False, f"cell {text!r} holds no number; the value is {v['value']!r}"
        if st not in CENSORED and v.get("bound") not in ("upper", "lower"):
            return False, f"cell {text!r} is a token, and the value {v['value']!r} is recorded as exact"
        return _same_number(v["value"], text), f"cell {text!r}, censored value {v['value']!r}"
    return _same_number(v["value"], text), f"cell {text!r}, value {v['value']!r}"


def _stated_by(text: str, r: dict, named: dict | None = None) -> bool:
    """Whether a record states the text of a cell no value of it cites: the target of a link (or the id the table's
    lookup gave the text), or one of the record's own properties (a text, an enumerated value, a reference, a number, a
    boolean)."""
    if "type" in r and (r.get("target") == text or (named and named.get(text) == r.get("target"))):
        return True
    if named and text in named and any(x == named[text] for k, x in (r.get("props") or {}).items()
                                       if isinstance(x, str) and k not in ("evidence", "stewardship")):
        return True
    num = parse_number(text)
    for k, x in (r.get("props") or {}).items():
        if k in ("evidence", "stewardship"):
            continue
        if isinstance(x, bool):
            if text.lower() == str(x).lower():
                return True
        elif isinstance(x, str) and x == text:
            return True
        elif isinstance(x, (int, float)) and num is not None and float(num) == float(x):
            return True
    return False


def _local_file(uri: Any, base: Path) -> Path | None:
    """A Dataset's uri as a local file: a relative path (to the document's folder) or a file: URI; None for anything
    else (a network URI is never fetched)."""
    if not isinstance(uri, str) or not uri.strip():
        return None
    if uri.startswith("file://"):
        return Path(uri[len("file://"):])
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", uri):
        return None
    p = Path(uri)
    return p if p.is_absolute() else base / p


def verify(doc: dict, files: dict[str, str | Path] | None = None, base: str | Path = ".") -> dict:
    """Each tabular Dataset that evidence cites, against a local copy of its bytes (``files``: Dataset id -> path, else
    its uri when that is a local path): ``verified`` when the file's sha256 is the one recorded and every cited cell
    holds what the record says, ``failed`` when it is not, ``unverified`` when no copy is at hand (never fetched)."""
    from .quantities import _values_anywhere
    files = {k: Path(v) for k, v in (files or {}).items()}
    datasets = _tables(doc)
    unknown = sorted(set(files) - set(datasets))
    if unknown:
        raise ScaleError(f"--file names {', '.join(unknown)}, which is no Dataset of the document")
    cited: dict[str, list[tuple[str, int, dict, list[tuple[str, dict]]]]] = {}
    records: dict[str, dict] = {}
    for r in [*(doc.get("nodes") or []), *(doc.get("links") or [])]:
        if isinstance(r, dict):
            records[str(r.get("id"))] = r
        props = r.get("props") if isinstance(r, dict) and isinstance(r.get("props"), dict) else {}
        values = list(_values_anywhere(props))
        for i, e in enumerate(_list(props.get("evidence"))):
            if isinstance(e, dict) and isinstance(e.get("dataset_cell"), dict) and isinstance(e.get("source"), str) \
                    and e["source"] in datasets:
                users = [(p, v) for p, v in values if isinstance(e.get("id"), str)
                         and e["id"] in (v.get("evidence_ids") or [] if isinstance(v.get("evidence_ids"), list) else [])]
                cited.setdefault(e["source"], []).append((str(r.get("id")), i, e, users))
    out = []
    for did in sorted(set(cited) | set(files)):
        d = datasets[did]
        items = cited.get(did, [])
        entry: dict = {"dataset": did, "cells": len(items), "recorded_sha256": d.get("sha256")}
        if not isinstance(d.get("table"), dict) and did in files:
            # a file no cell names (a workbook its tables were extracted from): its bytes, nothing else
            got = sha256(Path(files[did]).read_bytes()) if files[did].is_file() else None
            out.append({**entry, "file": str(files[did]), "file_sha256": got,
                        **({"status": "verified", "reason": "checksum only: the file has no table of its own"}
                           if got == d.get("sha256") else
                           {"status": "unverified", "reason": f"no local copy of the file (looked for {files[did]})"}
                           if got is None else
                           {"status": "failed", "problems": ["checksum mismatch: the file is not the bytes the Dataset "
                                                             "records (new bytes are a new Dataset)"]})})
            continue
        t = d.get("table") if isinstance(d.get("table"), dict) else {}
        ingest = t.get("ingest") if isinstance(t.get("ingest"), dict) else {}
        path = files.get(did) or _local_file(d.get("uri"), Path(base))
        data = None
        src = ingest.get("extracted_from")
        if (path is None or not Path(path).is_file()) and isinstance(src, str) and src in datasets:
            # a table read from a workbook (or another dialect): extract it again from the original file
            fp = files.get(src) or _local_file(datasets[src].get("uri"), Path(base))
            if fp is not None and Path(fp).is_file():
                raw = Path(fp).read_bytes()
                entry.update(source_dataset=src, source_file=str(fp), source_file_sha256=sha256(raw))
                if sha256(raw) != datasets[src].get("sha256"):
                    out.append({**entry, "status": "failed", "problems": [
                        f"checksum mismatch: {fp} is not the bytes {src} records (new bytes are a new Dataset)"]})
                    continue
                from .datainput import extract, recipe_from_ingest
                try:
                    data = extract(raw, recipe_from_ingest(ingest), str(fp))["canonical"]
                except ScaleError as exc:
                    out.append({**entry, "status": "failed", "problems": [f"{src}: {exc}"]})
                    continue
                path = fp
            else:
                path = fp
        if data is None and (path is None or not Path(path).is_file()):
            out.append({**entry, "status": "unverified",
                        "reason": "no local copy of the file" + (f" (looked for {path})" if path else
                                                                  "; give it with --file <dataset id>=<path>")})
            continue
        if data is None:
            data = Path(path).read_bytes()
        got = sha256(data)
        if "source_dataset" in entry:
            entry.update(table_sha256=got)          # the table extracted from the file (its recipe in table.ingest)
        else:
            entry.update(file=str(path), file_sha256=got)
        if got != d.get("sha256"):
            out.append({**entry, "status": "failed", "problems": ["checksum mismatch: the "
                                                                    + ("table extracted from the file" if "source_dataset"
                                                                       in entry else "file")
                                                                    + " is not the bytes the Dataset records (new bytes "
                                                                    "are a new Dataset)"]})
            continue
        named_by: dict[str, dict] = {}              # column -> {cell text: the id the table's lookup gave it}
        for k, x in sorted(ingest.items()):
            if k.startswith("lookup_") and isinstance(x, str):
                try:
                    col, text, rid = json.loads(x)
                except (ValueError, TypeError):
                    continue
                if isinstance(col, str) and isinstance(text, str) and isinstance(rid, str):
                    named_by.setdefault(col, {})[text] = rid
        try:
            header, body = read_table(data, t.get("profile", "scale_csv_v1"), str(path))
        except ScaleError as exc:
            out.append({**entry, "status": "failed", "problems": [str(exc)]})
            continue
        problems: list[str] = []
        if isinstance(t.get("rows"), int) and t["rows"] != len(body):
            problems.append(f"the table records {t['rows']} rows, the file has {len(body)}")
        names = [c.get("name") for c in _list(t.get("columns")) if isinstance(c, dict)]
        if names and names != header:
            problems.append(f"the table's columns {names} are not the file's header {header}")
        checked = matched = compared_quote = uncompared = 0
        index: dict = {}
        for rid, i, e, users in items:
            cell = e["dataset_cell"]
            where = f"{rid} evidence[{i}]"
            row, why = _row_of(cell, header, body, index)
            if row is None:
                problems.append(f"{where}: {why}")
                continue
            if not isinstance(cell.get("column"), str) or cell["column"] not in header:
                problems.append(f"{where}: column {cell.get('column')!r} is not in the file")
                continue
            text = row[header.index(cell["column"])]
            checked += 1
            ok = True
            if isinstance(e.get("quote"), str):
                compared_quote += 1
                if e["quote"] != text:
                    ok = False
                    problems.append(f"{where}: the quote {e['quote']!r} is not the cell's text {text!r}")
            numeric = (_columns(t).get(cell["column"]) or {}).get("datatype") in NUMERIC
            inconclusive = False
            named = named_by.get(cell["column"])
            for p, v in users:
                same, said = _holds(text, v, numeric, named)
                if same is False:
                    ok = False
                    problems.append(f"{where}: props{p} does not hold the cell: {said}")
                elif same is None:
                    uncompared += 1
                    inconclusive = True
            if not users and not _stated_by(text, records[rid], named):
                ok = False
                problems.append(f"{where}: no value cites the cell, and the record states nothing it holds ({text!r})")
            matched += ok and not inconclusive
        entry.update(checked=checked, matched=matched, quotes_compared=compared_quote,
                     **({"values_not_compared": uncompared} if uncompared else {}))
        status = "failed" if problems else "unverified" if uncompared else "verified"   # a value not compared proves nothing
        out.append({**entry, "status": status, **({"problems": problems} if problems else {}),
                    **({"reason": f"{uncompared} cited values could not be compared with their cells (a range)"}
                       if uncompared and not problems else {})})
    statuses = {x["status"] for x in out}
    overall = "failed" if "failed" in statuses else "unverified" if "unverified" in statuses or not out else "verified"
    return {"status": overall, "datasets": out}
