"""``scale data match``: the built-in mapping that fits a data file, with what is uncertain about it.

End users do not write mappings. The repository carries a library of reviewed mapping templates
(``domain/data-mappings/*.yaml``, :data:`scale.layout.DATA_MAPPINGS`): each names the column *roles* of one kind of
table (a run id, a potential, the Faradaic efficiencies of a wide table, ...), with header synonyms, the patterns of
wide columns (``FE CO (%)``), the quantity each numeric role holds and its default unit, and a ``mapping_version: 2``
skeleton whose placeholders the roles fill. :func:`match` reads a file (a CSV of any common dialect, or a sheet of an
.xlsx workbook; :mod:`scale.datainput`), scores every template against its header and a sample of its values, binds
each role to a column, and renders a complete mapping the user saves and passes to ``scale data add``.

Scoring (deterministic; the same file and library give the same answer):

* a role's score for a column is how well the header's name (its unit set aside) matches one of the role's synonyms:
  1.0 for the same words, 0.95 for the same words in another order, 0.75 when the header adds a word or two, 0.6 x the
  string similarity when that is at least 0.85; times 0.2 when the column's values are not of the role's kind (numbers,
  dates), 0 when the header's unit is of another dimension than the role's quantity, +0.05 when it is of the right one;
* a wide role (``melt``) takes every column whose header matches one of its patterns, its unit of the right dimension
  and its values numbers; the pattern's ``target`` group names the product;
* roles are bound greedily, best score first; a role whose best column has a rival within 0.05 is **not bound** (two
  columns fit: reported, never guessed); a role bound below 0.9 is bound and reported as uncertain;
* a template's score is 0.75 x the mean score of its required roles (0 for one unbound) + 0.25 x the share of the file's
  columns it binds.

Uncertain items (a binding by a similar name, a rival column, a unit assumed because the header writes none, tokens in
a numeric column the mapping does not name, a header row found rather than given, a sheet chosen among several) and
the template's own assumptions to confirm (``confirm``) are returned with the mapping. A mapping whose required roles
are not all bound, or that does not load, is returned with ``complete: false`` and placeholders (``?CHOOSE``) that
``scale data add`` refuses. ``bind`` (role -> column) settles a binding.
"""

from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import layout
from .dataingest import DATETIME, header_unit, load_mapping
from .datainput import RECIPE_KEYS, extract, find_header_row, grid, parse_range
from .datatable import parse_number
from .util import ScaleError, load_yaml

TEMPLATE_VERSION = 1
TEMPLATE_KEYS = {"template_version", "id", "title", "description", "maturity", "driving_forces", "default_origin",
                 "example", "roles", "confirm", "mapping", "require_one_of"}
ROLE_KEYS = {"kind", "required", "synonyms", "patterns", "quantity", "default_unit", "id", "exclude_targets", "values",
             "target_as", "definition"}
KINDS = ("text", "number", "datetime", "melt")
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)(?::([a-z_|]+))?\}\}")
CHOOSE = "?CHOOSE"
AMBIGUITY = 0.05
SURE = 0.9
MIN_SCORE = 0.5


def _fail(msg: str) -> None:
    raise ScaleError(msg)


# ------------------------------------------------------------------------------------------ the library
def library_dir(repo: Path) -> Path:
    return Path(repo) / layout.DATA_MAPPINGS


def load_templates(repo: Path) -> list[dict]:
    """The built-in mapping templates, checked for their shape, by id."""
    d = library_dir(repo)
    if not d.is_dir():
        _fail(f"{d}: no mapping library (domain/data-mappings)")
    out = []
    for p in sorted(d.glob("*.yaml")):
        out.append(check_template(load_yaml(p), p.name))
    ids = [t["id"] for t in out]
    if len(set(ids)) != len(ids):
        _fail("domain/data-mappings: two templates have one id")
    return sorted(out, key=lambda t: t["id"])


def check_template(t: Any, where: str) -> dict:
    if not isinstance(t, dict) or set(t) - TEMPLATE_KEYS:
        _fail(f"{where}: a template holds {sorted(TEMPLATE_KEYS)}")
    if t.get("template_version") != TEMPLATE_VERSION:
        _fail(f"{where}: template_version {TEMPLATE_VERSION} is required")
    for k in ("id", "title", "description", "mapping", "roles"):
        if k not in t:
            _fail(f"{where}: {k} is required")
    if t.get("default_origin", "recorded") not in ("recorded", "computed", "reported"):
        _fail(f"{where}: default_origin is recorded, computed or reported")
    for g in t.get("require_one_of") or []:
        if not isinstance(g, list) or not g or any(x not in t["roles"] for x in g):
            _fail(f"{where}: require_one_of is a list of lists of the template's roles")
    for name, r in t["roles"].items():
        w = f"{where}: roles.{name}"
        if not isinstance(r, dict) or set(r) - ROLE_KEYS:
            _fail(f"{w} holds {sorted(ROLE_KEYS)}")
        if r.get("kind") not in KINDS:
            _fail(f"{w}.kind is one of {list(KINDS)}")
        if r["kind"] == "melt":
            if not r.get("patterns") or not r.get("id"):
                _fail(f"{w}: a melt role names its patterns (each with a target group) and its id")
            if r.get("target_as", "target") not in ("target", "definition"):
                _fail(f"{w}.target_as is target (a record the lookup finds) or definition (the entry's definition)")
            for p in r["patterns"]:
                try:
                    rx = re.compile(p, re.IGNORECASE)
                except re.error as exc:
                    raise ScaleError(f"{w}: pattern {p!r} ({exc})") from exc
                if "target" not in rx.groupindex:
                    _fail(f"{w}: pattern {p!r} has no (?P<target>...) group")
        elif not r.get("synonyms"):
            _fail(f"{w}: synonyms are required")
    return t


# ------------------------------------------------------------------------------------------ scoring
def norm(text: str) -> str:
    t = text.lower().replace("Δ", " delta ").replace("δ", " delta ")
    t = re.sub(r"[_\-./:,;#()\[\]°]+", " ", t)
    return " ".join(t.split())


def name_score(header: str, synonyms: list[str]) -> float:
    """How well a header's name (its unit set aside) matches a role's synonyms (see the module docstring)."""
    h = norm(header_unit(header)[0])
    if not h:
        return 0.0
    best = 0.0
    hs = set(h.split())
    for syn in synonyms:
        n = norm(syn)
        if h == n:
            return 1.0
        ns = set(n.split())
        if hs == ns:
            best = max(best, 0.95)
        elif ns and ns <= hs and len(hs - ns) <= 2 and (len(n) >= 2 or len(hs) <= 2):
            best = max(best, 0.8 - 0.05 * len(hs - ns))
        else:
            r = SequenceMatcher(None, h, n).ratio()
            if r >= 0.85:
                best = max(best, round(0.6 * r, 3))
    return round(best, 3)


_TOKEN_NUMBER = re.compile(r"^[<>]\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _values(body: list[list[str]], j: int) -> list[str]:
    return [r[j] for r in body if r[j].strip()]


def _numeric_share(vals: list[str]) -> float:
    if not vals:
        return 0.0
    return sum(1 for v in vals if parse_number(v) is not None or _TOKEN_NUMBER.match(v)) / len(vals)


def _unit_check(spec: dict | None, quantity: str | None, unit: str | None) -> tuple[str, str | None]:
    """('ok' | 'wrong' | 'unread' | 'none', why) for a header's unit against a quantity kind."""
    if unit is None:
        return "none", None
    if not quantity or spec is None:
        return "ok", None
    from .datatable import _kind_dims_error
    from .quantities import contracts
    c = contracts(spec)
    if c is None or quantity not in c.kinds:
        return "ok", None
    reading = c.read_unit(unit, tuple(x["context"] for x in c.kinds[quantity]["compare_by"]))
    if not reading.read:
        other = c.read_unit(unit, tuple(c.contexts))  # a unit of another kind's context (V vs RHE), read as that
        if other.read and other.vector not in c.kinds[quantity]["dimensions"]:
            return "wrong", f"unit {unit!r} is {c.dimension_name(other.vector)}, not a dimension of {quantity}"
        return "unread", f"the unit {unit!r} cannot be read as a {quantity}"
    why = _kind_dims_error(spec, quantity, unit)
    return ("wrong", why) if why else ("ok", None)


def _snake(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return t if re.match(r"^[a-z]", t) else f"x_{t}"


def _melt(role: str, r: dict, header: list[str], body: list[list[str]], free: list[int], spec: dict | None,
          uncertain: list[dict]) -> list[dict]:
    """The columns a wide role takes: header matches a pattern, unit of the right dimension, values numbers."""
    pats = [re.compile(p, re.IGNORECASE) for p in r["patterns"]]
    excl = {x.lower() for x in r.get("exclude_targets") or []}
    out, ids = [], set()
    for j in free:
        h = header[j]
        name, unit = header_unit(h)
        m = next((p.match(x) for x in (h.strip(), name) for p in pats if p.match(x)), None)
        if m is None:
            continue
        target = m.group("target").strip()
        if not target or target.lower() in excl:
            continue
        if m.string == h.strip() and unit is not None:
            unit = None                              # the pattern read the whole header: its parenthesis is no unit
        state, why = _unit_check(spec, r.get("quantity"), unit)
        if state == "wrong":
            uncertain.append({"role": role, "column": h, "why": f"not taken: {why}", "blocking": True})
            continue
        vals = _values(body, j)
        if _numeric_share(vals) < 0.6:
            uncertain.append({"role": role, "column": h, "why": "not taken: its values are not numbers", "blocking": True})
            continue
        mid = f"{r['id']}_{_snake(target)}"
        k = 2
        while mid in ids:
            mid, k = f"{r['id']}_{_snake(target)}_{k}", k + 1
        ids.add(mid)
        entry = {"column": h, "id": mid, "target": target, "unit": unit if state == "ok" else None,
                 "unit_note": why if state == "unread" else None}
        out.append(entry)
    return out


def _bind(t: dict, header: list[str], body: list[list[str]], spec: dict | None, bind: dict) -> dict:
    roles = t["roles"]
    order = list(roles)
    uncertain: list[dict] = []
    claimed: dict[int, str] = {}
    bound: dict[str, dict] = {}
    melts: dict[str, list[dict]] = {}
    for role, col in (bind or {}).items():
        if role not in roles:
            continue
        if col not in header:
            _fail(f"--bind {role}={col}: the file's header has no column {col!r} ({header})")
        if roles[role]["kind"] == "melt":
            _fail(f"--bind {role}: a wide role takes its columns by their headers' pattern; it is not bound by hand")
        j = header.index(col)
        bound[role] = {"column": col, "score": 1.0, "by": "you"}
        claimed[j] = role
    # wide roles first: their patterns are specific
    for role in order:
        r = roles[role]
        if r["kind"] != "melt":
            continue
        free = [j for j in range(len(header)) if j not in claimed]
        cols = _melt(role, r, header, body, free, spec, uncertain)
        if cols:
            melts[role] = cols
            for c in cols:
                claimed[header.index(c["column"])] = role
    # then the single-column roles: every candidate scored
    scores: dict[str, dict[int, float]] = {}
    notes: dict[tuple[str, int], str] = {}
    for role in order:
        r = roles[role]
        if r["kind"] == "melt" or role in bound:
            continue
        for j, h in enumerate(header):
            if j in claimed:
                continue
            s = name_score(h, r["synonyms"])
            if s <= 0:
                continue
            vals = _values(body, j)
            if r["kind"] == "number":
                if _numeric_share(vals) < 0.6:
                    s, notes[(role, j)] = s * 0.2, "its values are not numbers"
                state, why = _unit_check(spec, r.get("quantity"), header_unit(h)[1])
                if state == "wrong":
                    s, notes[(role, j)] = 0.0, why
                elif state == "unread":
                    s, notes[(role, j)] = s * 0.7, why
                elif state == "ok" and header_unit(h)[1] is not None:
                    s = min(1.0, s + 0.05)
            elif r["kind"] == "datetime" and vals and sum(1 for v in vals if _DATEISH.match(v)) / len(vals) < 0.8:
                s, notes[(role, j)] = s * 0.2, "its values are not dates"
            if s > 0:
                scores.setdefault(role, {})[j] = round(s, 3)
    triples = sorted(((-s, order.index(role), j, role) for role, d in scores.items() for j, s in d.items()
                      if s >= MIN_SCORE))
    ambiguous: dict[str, list[int]] = {}
    for negs, _, j, role in triples:
        if role in bound or role in ambiguous or j in claimed:
            continue
        s = -negs
        rivals = [k for k, x in scores[role].items() if k != j and k not in claimed and x >= s - AMBIGUITY
                  and x >= MIN_SCORE]
        if rivals:
            ambiguous[role] = sorted([j, *rivals])
            continue
        bound[role] = {"column": header[j], "score": s, "by": "name"}
        claimed[j] = role
    for role, cols in ambiguous.items():
        if role in bound:
            continue
        uncertain.append({"role": role, "column": None, "why": "two or more columns fit; not bound (choose one with "
                          f"--bind {role}=<column>)", "candidates": [{"column": header[k], "score": scores[role][k]}
                                                                     for k in cols]})
    for role in order:
        r = roles[role]
        if role in bound and bound[role]["score"] < SURE:
            cands = sorted(((x, k) for k, x in (scores.get(role) or {}).items()), reverse=True)[:3]
            uncertain.append({"role": role, "column": bound[role]["column"],
                              "why": f"matched by a similar name (score {bound[role]['score']})",
                              "candidates": [{"column": header[k], "score": x} for x, k in cands]})
        if r.get("required") and role not in bound and role not in melts and role not in ambiguous:
            cands = sorted(((x, k) for k, x in (scores.get(role) or {}).items()), reverse=True)[:3]
            uncertain.append({"role": role, "column": None, "why": "required, and no column was found"
                              + "".join(f"; {notes[(role, k)]} ({header[k]!r})" for _, k in cands if (role, k) in notes),
                              "candidates": [{"column": header[k], "score": x} for x, k in cands]})
    return {"bound": bound, "melts": melts, "ambiguous": ambiguous, "uncertain": uncertain, "claimed": claimed}


def _units(t: dict, b: dict, header: list[str], spec: dict | None) -> tuple[dict, list[dict]]:
    """Each bound numeric role's unit: from its header (checked) or the template's default (reported)."""
    units, uncertain = {}, []
    for role, x in b["bound"].items():
        r = t["roles"][role]
        if r["kind"] != "number":
            continue
        u = header_unit(x["column"])[1]
        state, why = _unit_check(spec, r.get("quantity"), u)
        if state == "ok":
            units[role] = {"from_header": True, "unit": u}
        elif state == "wrong":
            units[role] = {"from_header": False, "unit": r.get("default_unit")}
            uncertain.append({"role": role, "column": x["column"], "why": f"{why}; bound by --bind"})
        else:
            units[role] = {"from_header": False, "unit": r.get("default_unit")}
            uncertain.append({"role": role, "column": x["column"],
                              "why": (f"{why}; " if why else "the header writes no unit; ")
                              + f"assumed {r.get('default_unit')!r}"})
    for role, cols in b["melts"].items():
        r = t["roles"][role]
        for c in cols:
            if c["unit"] is None:
                c["unit_assumed"] = r.get("default_unit")
                uncertain.append({"role": role, "column": c["column"],
                                  "why": (f"{c['unit_note']}; " if c.get("unit_note") else "the header writes no unit; ")
                                  + f"assumed {r.get('default_unit')!r}"})
    return units, uncertain


def _tokens(t: dict, b: dict, header: list[str], body: list[list[str]]) -> list[dict]:
    """Cells of the numeric columns that are neither numbers, nor blanks, nor a '<' bound: the mapping names none of
    them, so scale data add would refuse them."""
    out = []
    cols = [(role, x["column"]) for role, x in b["bound"].items() if t["roles"][role]["kind"] == "number"]
    cols += [(role, c["column"]) for role, cs in b["melts"].items() for c in cs]
    for role, col in cols:
        j = header.index(col)
        bad = sorted({v for v in _values(body, j) if parse_number(v) is None
                      and not re.match(r"^<\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$", v)})
        if bad:
            out.append({"role": role, "column": col, "why": f"cells {bad[:5]} are no number, blank or '<' bound: name "
                        "them under missing (not_measured, not_reported) or correct the file", "tokens": bad[:20],
                        "blocking": True})
    for role, x in b["bound"].items():
        allowed = t["roles"][role].get("values")
        if allowed:
            j = header.index(x["column"])
            bad = sorted({v for v in _values(body, j) if v not in allowed})
            if bad:
                out.append({"role": role, "column": x["column"], "why": f"cells {bad[:5]} are not one of {allowed}: "
                            "correct the column or leave it unbound", "tokens": bad[:20], "blocking": True})
        if t["roles"][role]["kind"] == "datetime":
            j = header.index(x["column"])
            bad = [v for v in _values(body, j) if not DATETIME.fullmatch(v)]
            if bad:
                out.append({"role": role, "column": x["column"], "why": f"times such as {bad[0]!r} are not ISO 8601 "
                            "date-times with a time zone (2026-03-02T10:00:00Z); correct the column or leave it unbound",
                            "blocking": True})
    return out


# ------------------------------------------------------------------------------------------ rendering
def _render(t: dict, b: dict, units: dict, ns: str, origin: str, recorded_at: str | None, recipe: dict) -> Any:
    bound, melts = b["bound"], b["melts"]

    def is_bound(role: str) -> bool:
        return role in bound or role in melts

    def cond(w: Any) -> bool:
        ws = w if isinstance(w, list) else [w]
        return all((not is_bound(x[1:])) if x.startswith("!") else any(is_bound(y) for y in x.split("|"))
                   for x in ws)

    def column(roles: str) -> Any:
        for role in roles.split("|"):
            if role in bound:
                return bound[role]["column"]
        role = roles.split("|")[0]
        if t["roles"][role].get("required"):
            cands = [c["column"] for c in next((u.get("candidates") or [] for u in b["uncertain"] if u["role"] == role
                                                and u.get("candidates")), [])]
            return f"{CHOOSE}: a column for {role}" + (f" ({' | '.join(cands)})" if cands else "")
        return DROP

    def melt(role: str) -> Any:
        out = []
        for c in melts.get(role) or []:
            r = t["roles"][role]
            if r.get("target_as") == "definition":
                e = {"column": c["column"], "id": c["id"],
                     "definition": (r.get("definition") or "{target}").replace("{target}", c["target"])}
            else:
                e = {"column": c["column"], "id": c["id"], "target": c["target"]}
            if not all(x["unit"] for x in melts[role]):
                e["unit"] = c["unit"] or c.get("unit_assumed")
            out.append(e)
        return out or DROP

    def value(s: str) -> Any:
        m = PLACEHOLDER.fullmatch(s)
        if m:
            kind, arg = m.group(1), m.group(2)
            if kind == "column":
                return column(arg)
            if kind == "melt":
                return melt(arg)
            if kind == "unit":
                if arg in melts:
                    return HEADER if all(c["unit"] for c in melts[arg]) else DROP
                u = units.get(arg)
                return DROP if u is None else HEADER if u["from_header"] else u["unit"]
            if kind == "input":
                return dict(recipe)
            if kind == "origin":
                return origin
            if kind == "recorded_at":
                return recorded_at if recorded_at else DROP
            if kind == "ns":
                return ns
        return PLACEHOLDER.sub(lambda mm: ns if mm.group(1) == "ns" else mm.group(0), s)

    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            if "when" in x and not cond(x["when"]):
                return DROP
            out = {}
            for k, v in x.items():
                if k == "when":
                    continue
                w = walk(v)
                if w is DROP:
                    continue
                if w is HEADER and k == "unit":
                    out["unit_from_header"] = True
                    continue
                out[k] = w
            return out
        if isinstance(x, list):
            return [w for w in (walk(v) for v in x) if w is not DROP]
        if isinstance(x, str):
            return value(x)
        return x

    m = walk(copy.deepcopy(t["mapping"]))
    for r in m.get("records") or []:                 # a lookup whose column is not bound has nothing to look up
        for s in r.get("results") or []:
            if "target_column" not in s and "melt" not in s:
                s.pop("target_lookup", None)
        for ln in r.get("links") or []:
            if "target_column" not in ln:
                ln.pop("target_lookup", None)
        for p in (r.get("properties") or {}).values():
            if isinstance(p, dict) and "ref_column" not in p:
                p.pop("lookup", None)
    if origin != "recorded":                         # a recording time goes only with first-hand records
        for r in m.get("records") or []:
            r.pop("recorded_at", None)
            r.pop("recorded_at_column", None)
    return m


class _Sentinel:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


DROP, HEADER = _Sentinel("DROP"), _Sentinel("HEADER")


def to_yaml(m: dict, comments: list[str]) -> str:
    import yaml
    body = yaml.safe_dump(m, sort_keys=False, allow_unicode=True, width=120, default_flow_style=False)
    body = re.sub(r"\? ''\n(\s*): ", lambda x: "'':\n" + x.group(1) + "  ", body)   # a blank token as a plain key
    return "".join(f"# {c}\n" for c in comments) + body


# ------------------------------------------------------------------------------------------ reading the file
def _detect_decimal(rows: list[list[str]], delimiter: str | None) -> str | None:
    if delimiter not in (";", "\t"):
        return None
    cells = [x.strip() for r in rows for x in r]
    comma = sum(1 for x in cells if re.fullmatch(r"[+-]?\d+,\d+", x))
    dot = sum(1 for x in cells if re.fullmatch(r"[+-]?\d+\.\d+", x))
    return "," if comma and not dot else None


def read(data: bytes, file_name: str, sheet: str | None = None, header_row: int | None = None,
         options: dict | None = None) -> list[dict]:
    """The tables a file offers: one per sheet (only ``sheet`` when given), each extracted at its header row (given, or
    found). Each: ``{recipe, header, body, sheets?, notes, error?}``."""
    opts = {k: v for k, v in (options or {}).items() if k in RECIPE_KEYS and v is not None}
    g = grid(data, {**opts, **({"sheet": sheet} if sheet else {})})
    sheet_names = [sheet] if sheet else (g.get("sheets") or [None])
    out = []
    for sh in sheet_names:
        o = {**opts}
        if sh:
            o["sheet"] = sh
        gg = g if (sh is None or sh == g.get("sheet")) else grid(data, o)
        rows = gg["rows"]
        if o.get("range"):
            r0, c0, r1, c1 = parse_range(o["range"])
            rows = [r[c0:c1 + 1] for r in rows[r0:r1 + 1]]
        notes: list[dict] = []
        hr = header_row
        if hr is None:
            found, cands = find_header_row(rows)
            if found is None:
                out.append({"sheet": sh, "error": "no header row found (a row of names followed by a row with numbers); "
                            "give --header-row", "recipe": {}, "header": [], "body": []})
                continue
            hr = found + (parse_range(o["range"])[0] if o.get("range") else 0)
            if found != 1 or len(cands) > 1:
                notes.append({"role": None, "column": None, "why": f"the header row was found, not given: row {hr}"
                              + (f" (other candidates: {cands[1:4]})" if len(cands) > 1 else "")
                              + "; give --header-row to settle it"})
        if gg["format"] == "csv" and "decimal" not in o:
            dec = _detect_decimal(rows[hr - 1:], gg.get("delimiter"))
            if dec:
                o["decimal"] = dec
                notes.append({"role": None, "column": None, "why": "decimal commas read as decimal points (12,5 as "
                              "12.5); the mapping records input.decimal"})
        if gg["format"] == "csv":
            o.setdefault("encoding", gg["encoding"])
            o.setdefault("delimiter", gg["delimiter"])
        o["header_row"] = hr
        o["format"] = gg["format"]
        try:
            e = extract(data, o, file_name)
        except ScaleError as exc:
            out.append({"sheet": sh, "error": str(exc), "recipe": o, "header": [], "body": []})
            continue
        if e["ignored_after"]:
            notes.append({"role": None, "column": None, "why": f"{e['ignored_after']} non-empty rows after a blank row "
                          f"are not read (cells {e['rows_used']} are); give input.range to read another block"})
        recipe = {k: e["recipe"][k] for k in RECIPE_KEYS if k in e["recipe"]}
        out.append({"sheet": sh, "recipe": recipe, "header": e["header"], "body": e["body"], "notes": notes,
                     "cells": e["rows_used"], **({"sheets": e["sheets"]} if "sheets" in e else {})})
    return out


# ------------------------------------------------------------------------------------------ matching
def match(data: bytes, file_name: str, templates: list[dict], spec: dict | None = None, sheet: str | None = None,
          header_row: int | None = None, template: str | None = None, bind: dict | None = None, namespace: str = "lab",
          origin: str | None = None, recorded_at: str | None = None, top: int = 3, options: dict | None = None,
          samples: bool = True) -> dict:
    """The templates that fit a file, best first, each with its bindings, its uncertain items and a mapping (YAML)."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", namespace or ""):
        _fail("--namespace is a short lower-case prefix for the ids the mapping makes (lab, mygroup)")
    if origin is not None and origin not in ("recorded", "computed", "reported", "synthetic"):
        _fail("--origin is recorded, computed, reported or synthetic")
    if recorded_at is not None and not DATETIME.fullmatch(recorded_at):
        _fail("--recorded-at is an ISO 8601 date-time with a time zone (2026-03-02T10:00:00Z)")
    if template:
        templates = [t for t in templates if t["id"] == template]
        if not templates:
            _fail(f"no built-in mapping {template!r} (scale data match --list)")
    tables = read(data, file_name, sheet, header_row, options)
    candidates = []
    for tb in tables:
        if tb.get("error"):
            continue
        header, body = tb["header"], tb["body"]
        for t in templates:
            b = _bind(t, header, body, spec, bind or {})
            units, unc_units = _units(t, b, header, spec)
            toks = _tokens(t, b, header, body)
            groups = [[r] for r, x in t["roles"].items() if x.get("required")] + list(t.get("require_one_of") or [])
            s_req = sum(max(_got(b, r) for r in g) for g in groups) / max(1, len(groups))
            missing_any = [g for g in t.get("require_one_of") or [] if not any(_got(b, r) for r in g)]
            cov = len(b["claimed"]) / max(1, len(header))
            score = round(0.75 * s_req + 0.25 * cov, 3)
            orig = origin or t.get("default_origin", "recorded")
            m = _render(t, b, units, namespace, orig, recorded_at, tb["recipe"])
            uncertain = [*tb["notes"], *b["uncertain"], *unc_units, *toks]
            uncertain += [{"role": None, "column": None, "why": f"none of {', '.join(g)} was found: the mapping would "
                           "read nothing this kind of table is for", "blocking": True} for g in missing_any]
            if len([x for x in tables if not x.get("error")]) > 1:
                uncertain.insert(0, {"role": None, "column": None, "why": f"sheet {tb['sheet']!r} of "
                                     f"{', '.join(repr(x['sheet']) for x in tables)}; give --sheet to choose another"})
            if orig == "recorded" and "recorded_at" not in b["bound"] and not recorded_at:
                uncertain.append({"role": "recorded_at", "column": None, "why": "origin recorded needs the time the data "
                                  "were recorded: bind a date-time column (--bind recorded_at=<column>) or give "
                                  "--recorded-at; a table from a publication is --origin reported"})
            problems = []
            try:
                load_mapping(to_yaml(m, []).encode("utf-8"))
            except ScaleError as exc:
                problems.append(str(exc))
            complete = (not problems and not any(isinstance(v, str) and v.startswith(CHOOSE) for v in _strings(m))
                        and not any(u.get("blocking") for u in uncertain))
            unmapped = [h for j, h in enumerate(header) if j not in b["claimed"]]
            comments = [f"scale data match: built-in mapping {t['id']} ({t['title']}), score {score}",
                        f"file {file_name}" + (f", sheet {tb['sheet']!r}" if tb.get("sheet") else "")
                        + f", cells {tb['cells']}"]
            if not complete:
                comments.append("INCOMPLETE: settle the ?CHOOSE placeholders and the items below before scale data add")
            comments += ["uncertain: " + (f"{u.get('column') or u.get('role')}: " if u.get("column") or u.get("role")
                                          else "") + u["why"] for u in uncertain]
            comments += [f"confirm: {c}" for c in t.get("confirm") or []]
            if unmapped:
                comments.append(f"not mapped: {', '.join(unmapped)}")
            candidates.append({
                "template": t["id"], "title": t["title"], "score": score, "sheet": tb.get("sheet"),
                "complete": complete,
                "bindings": {r: {**x, **({"unit": units[r]["unit"], "unit_from_header": units[r]["from_header"]}
                                        if r in units else {})} for r, x in b["bound"].items()},
                "melt": {r: [{k: v for k, v in c.items() if v is not None and k != "unit_note"} for c in cs]
                         for r, cs in b["melts"].items()},
                "uncertain": uncertain, "confirm": list(t.get("confirm") or []), "unmapped_columns": unmapped,
                "problems": problems, "mapping": m, "mapping_yaml": to_yaml(m, comments)})
    candidates.sort(key=lambda c: (-c["score"], c["template"], str(c["sheet"])))
    reading = []
    for tb in tables:
        x = {"sheet": tb.get("sheet"), **({"error": tb["error"]} if tb.get("error") else {}),
             "recipe": tb.get("recipe"), "header": tb["header"], "rows": len(tb["body"]),
             **({"cells": tb["cells"]} if tb.get("cells") else {})}
        if samples and tb["header"]:
            x["samples"] = {h: [r[j] for r in tb["body"][:3]] for j, h in enumerate(tb["header"])}
        reading.append(x)
    return {"file": file_name, "reading": reading, "candidates": candidates[:max(1, top)],
            "best": candidates[0]["template"] if candidates else None}


def _got(b: dict, role: str) -> float:
    """A role's score in a binding: its column's, 1.0 for a wide role that took columns, 0 when unbound."""
    return (b["bound"].get(role) or {}).get("score", 1.0 if role in b["melts"] else 0.0)


def _strings(x: Any):
    if isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)
    elif isinstance(x, str):
        yield x


def describe(templates: list[dict]) -> list[dict]:
    """The library, for ``scale data match --list``."""
    return [{"id": t["id"], "title": t["title"], "driving_forces": t.get("driving_forces") or [],
             "roles": {r: {"kind": x["kind"], "required": bool(x.get("required")),
                           **({"quantity": x["quantity"]} if x.get("quantity") else {}),
                           **({"default_unit": x["default_unit"]} if x.get("default_unit") else {})}
                       for r, x in t["roles"].items()},
             "example": t.get("example")} for t in templates]
