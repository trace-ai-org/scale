"""``scale add-source``: any source into a graph, by one entry point (docs/data.md, "Your own data, from any source").

    scale add-source FILE_OR_DOI --into DOC [--dry-run] [--kind table|document|graph]
                     [--sheet S] [--header-row N] [--recorded-at ISO] [--origin O] [--bind ROLE=COLUMN]...
                     [--confirm ID]... [--template ID] [--namespace NS] [--from-source SOURCE_ID [--locator TEXT]]
                     [--doi DOI] [--email ADDRESS] [--source-kind KIND] [--merge]
    scale add-source --finish FOLDER --into DOC [--dry-run]

The router reads what the source is and sends it the way that kind goes (:func:`detect`):

* **a table** (a CSV file of any common dialect, an .xlsx workbook; a text file laid out in columns): the built-in
  mapping that fits it (:mod:`scale.datamatch`). When the match leaves nothing uncertain, the file is imported into
  DOC (:mod:`scale.dataingest`, validated, whole or not at all) and every cited cell checked against the file
  (:func:`scale.datatable.verify`); otherwise nothing is written and the uncertain items come back as **questions**,
  each with the exact option (or tool argument) that settles it. Nothing is guessed: a binding by a similar name, two
  columns that fit one role, a unit the header does not write, a recording time, a sheet among several that fit, and
  cells no rule reads are asked about; the notes (a header row found, decimal commas) and the template's assumptions
  are reported. Inside a data repository a file from outside it is copied into its ``raw/`` (kept on the computer) so
  that the Dataset's uri stays good. ``--from-source`` records that the table is the data of a publication DOC holds:
  the Dataset cites the Source (a curation note at ``--locator``) and is REPORTED_IN it.
* **a document** (a PDF; a DOI; a text file of a paper, patent, thesis or report): a curation folder is started next
  to DOC (in the data repository's ``work/sources/``, kept on the computer, else ``work/sources/`` beside DOC) by
  ``scale source start``, with DOC's stewardship and project; the reading is the agent's, by the curate-corpus skill.
  ``--finish FOLDER`` then folds the checked graph into DOC: records keep their ids, the Source's rights entry goes to
  the destination's ``rights.yaml``, and the result must validate.
* **a graph** (a scale-catal document, .json): validated; merged into DOC only with ``--merge``. A record DOC holds
  with other content is a conflict and refuses the merge; records keep their own audience (an incoming header's
  stewardship is written on each record where DOC's header would give it another).

Writing is refused inside the SCALE framework repository (:func:`scale.datarepo.refuse_framework`), and, in a data
repository, when the result would hold a record some reader of the repository may not read (its audience rule).
Network access is only in ``scale source start`` (a DOI), through ``get`` (replaceable, for tests).
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from . import curate, datamatch, pgraph
from .audience import audience_problems, find_policy, parse_policy
from .dataingest import DATETIME, load_mapping, plan, write_atomic
from .datarepo import refuse_framework
from .util import ScaleError, dump_yaml, find_repo_root, load_doc, load_yaml

ROUTES = ("table", "document", "graph")
TABLE_EXT = {".csv", ".tsv", ".xlsx", ".xlsm"}
UNSUPPORTED = {".xls": "an Excel 97-2003 workbook: save it as .xlsx (or export the sheet as CSV)",
               ".ods": "an OpenDocument spreadsheet: save it as .xlsx (or export the sheet as CSV)",
               ".docx": "a Word document: export it as PDF (or save its text as .txt)",
               ".doc": "a Word document: export it as PDF (or save its text as .txt)",
               ".odt": "an OpenDocument text: export it as PDF (or save its text as .txt)",
               ".pptx": "a presentation: export it as PDF",
               ".parquet": "a Parquet table: export it as CSV", ".h5": "an HDF5 file: export the table as CSV",
               ".zip": "an archive: unpack it and add its files one by one"}
SOURCE_KINDS = ("article", "patent", "thesis", "report", "book", "preprint", "dataset_paper", "lab_notebook", "other")
DATA_RIGHTS = ("own", "open", "public_domain", "restricted", "in_copyright")


def _data_rights(choice: str | None, origin: str | None, name: str) -> dict:
    """The rights entry of a Dataset the router writes. ``own`` (the default for data the project recorded or
    computed): the project's own measurements, whose cells no third party's copyright covers, so they are quoted
    uncapped (class open); a publication's table takes the class of its licence, as the person states it."""
    if (choice or "own") == "own":
        return {"class": "open", "licence": "the project's own data (no third-party licence)",
                "basis": f"{name}: data the project {origin or 'recorded'}; added by scale add-source"}
    return {"class": choice, "licence": "as stated by the person who added the file",
            "basis": f"{name}: stated with scale add-source --data-rights {choice}"}


_RIGHTS_Q = {"id": "rights", "about": "the rights of the table",
             "why": "a table of a publication is quoted cell by cell: its rights decide how much may be kept verbatim "
                    "(in_copyright caps the quotes; open or public_domain when its licence says so; own when it is "
                    "your own data that you published)",
             "settle": {"options": ["--data-rights CLASS", *(f"--data-rights {c}" for c in
                                                             ("in_copyright", "open", "public_domain", "own"))],
                        "tool_arguments": {"data_rights": "CLASS"}},
             "candidates": ["in_copyright", "open", "public_domain", "own"]}
# uncertain items of a match that are notes (reported, not asked): how the file was read
NOTE = re.compile(r"^(the header row was found|decimal commas|cells formatted as percentages|\d+ non-empty rows after)")


def _fail(msg: str) -> None:
    raise ScaleError(msg)


# ------------------------------------------------------------------------------------------ what the source is
def _looks_tabular(text: str) -> bool:
    """Text whose non-empty lines (at least three) nearly all hold the same number, one or more, of one delimiter."""
    lines = [ln for ln in text.splitlines() if ln.strip()][:200]
    if len(lines) < 3:
        return False
    for d in ("\t", ";", ","):
        counts = [ln.count(d) for ln in lines]
        top = max(set(counts), key=counts.count)
        if top >= 1 and counts.count(top) >= 0.8 * len(counts):
            return True
    return False


def detect(source: str, kind: str | None = None) -> dict:
    """What ``source`` is: ``{route: table|document|graph, path?, doi?, format}``. A DOI (10.x/..., doi:, a doi.org
    link) that names no file is a document; a file is read by its first bytes and its name (a PDF's magic, a
    workbook's zip, a JSON document, a CSV or text laid out in columns, else the text of a document). ``kind`` (one of
    :data:`ROUTES`) overrides the reading of a file."""
    if kind is not None and kind not in ROUTES:
        _fail(f"--kind is one of {', '.join(ROUTES)}")
    p = Path(str(source)).expanduser()
    if not p.exists():
        try:
            doi = curate.normalize_doi(str(source))
        except ScaleError:
            _fail(f"{source}: no such file, and not a DOI (10.<registrant>/<suffix>, doi:..., or a doi.org link)")
        if kind not in (None, "document"):
            _fail(f"{source} is a DOI: it is added as a document (a publication to curate)")
        return {"route": "document", "doi": doi, "format": "doi"}
    if p.is_dir():
        _fail(f"{p} is a folder: give one file (a curation folder is folded in by --finish {p})")
    data = p.read_bytes()
    ext = p.suffix.lower()
    if data[:4] == b"%PDF":
        fmt = "pdf"
    elif data[:4] == b"PK\x03\x04":
        from .datainput import sniff
        fmt = sniff(data)                              # xlsx, or a ScaleError saying what the zip is not
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        _fail(f"{p}: an Excel 97-2003 workbook (.xls) or another Office binary file: save it as .xlsx or PDF")
    elif ext in UNSUPPORTED:
        _fail(f"{p}: {UNSUPPORTED[ext]}")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = None
            if ext in TABLE_EXT:
                fmt = "csv"                            # another encoding: the reader detects it
            else:
                _fail(f"{p}: not a PDF, a workbook, a CSV file, a JSON graph or UTF-8 text; give --kind, or export it "
                      "as PDF, CSV or .xlsx")
        if text is not None:
            if ext == ".json" or (ext not in TABLE_EXT and text.lstrip().startswith("{")):
                fmt = "json"
            elif ext in TABLE_EXT or _looks_tabular(text):
                fmt = "csv"
            else:
                fmt = "text"
    route = kind or {"pdf": "document", "text": "document", "xlsx": "table", "csv": "table", "json": "graph"}[fmt]
    if route == "graph" and fmt != "json":
        _fail(f"{p}: --kind graph takes a scale-catal document (.json)")
    if route == "table" and fmt not in ("csv", "xlsx", "text"):
        _fail(f"{p}: --kind table takes a CSV file or an .xlsx workbook")
    if route == "document" and fmt not in ("pdf", "text", "csv"):
        _fail(f"{p}: --kind document takes a PDF or a text file")
    return {"route": route, "path": str(p.resolve()), "format": fmt}


# ------------------------------------------------------------------------------------------ the destination
def destination(into: str | Path, must_exist: bool = True) -> dict:
    """The document written to: refused inside the framework repository; its data repository (policy) if any."""
    p = Path(into).expanduser().resolve()
    refuse_framework(p, "a source added to a graph of project data")
    if p.suffix != ".json":
        _fail(f"--into {into}: the destination is a scale-catal document (.json)")
    if must_exist and not p.is_file():
        _fail(f"--into {into}: no such document; make one first: scale data new {into} --project <id> --contributor <id> "
              "(inside a data repository its defaults name them)")
    pf = find_policy(p)
    return {"path": p, "policy_file": pf, "policy": parse_policy(load_yaml(pf), str(pf)) if pf else None,
            "data_repository": pf.parent if pf else None}


def _spec(repo: Path, doc: dict) -> dict:
    return pgraph.load_spec(pgraph.spec_for(repo, (doc.get("graph") or {}).get("schema_version"), None, doc))


def _root(dest: dict) -> Path:
    """Where the router may write: the data repository, else the destination's folder."""
    return (dest["data_repository"] or dest["path"].parent).resolve()


def _safe(target: Path, dest: dict) -> Path:
    """A path the router writes, checked where it really lands (links followed): inside :func:`_root`, never in the
    framework repository."""
    t = Path(target).resolve()
    if not t.is_relative_to(_root(dest)):
        _fail(f"{target} leads to {t}, outside {_root(dest)}: scale add-source writes only there (a linked folder or "
              "file that leads elsewhere is refused)")
    refuse_framework(t, "the working material of a source")
    return Path(target)


def _work(dest: dict) -> Path:
    """Working material (curation folders, mappings): the data repository's work/ (git-ignored), else work/ beside DOC."""
    return _safe(Path(_root(dest)) / "work", dest)


def _audience_of(dest: dict, doc: dict) -> dict:
    """Who reads what the router adds: the destination document's header stewardship, which the records take."""
    return pgraph.stewardship_of((doc.get("graph") or {}).get("stewardship"))


def _audience(dest: dict, doc: dict) -> None:
    if dest["policy"] is not None:
        problems = audience_problems(doc, dest["path"].name, dest["policy"]["readers"])
        if problems:
            _fail("; ".join(problems[:5]) + ": the data repository's readers may not read it, so nothing was written")


def _source_audience(path: Path, dest: dict, doc: dict) -> None:
    """A file kept in a data repository carries that repository's default stewardship: it goes into a destination only
    when every reader of the destination may read it (an institution's file never enters a project's graph)."""
    pf = find_policy(path)
    if pf is None:
        return
    st = (parse_policy(load_yaml(pf), str(pf)).get("defaults") or {}).get("stewardship") or {"access": "institution"}
    probe = {"graph": {"stewardship": st}, "nodes": [], "links": []}
    if audience_problems(probe, path.name, _audience_of(dest, doc)):
        _fail(f"{path} is in the data repository {pf.parent}, whose data may be read by fewer readers than "
              f"{dest['path'].name}'s header admits; nothing was written (add it to a document of that repository, or have its "
              "partners clear it first)")


def _new_errors(before: dict, after: dict, spec: dict) -> tuple[list[dict], object]:
    old = {(e.get("record"), e.get("message")) for e in pgraph.validate(before, spec).errors}
    rep = pgraph.validate(after, spec)
    return [e for e in rep.errors if (e.get("record"), e.get("message")) not in old], rep


def _checklist(rep, ids: set[str]) -> dict:
    """The checklist's must items the records ``ids`` miss, by checklist: what is still to be recorded."""
    out = {}
    for ref, c in ((rep.conformance or {}).get("profiles") or {}).items():
        mine = [v for v in c.get("violations") or [] if v.get("record") in ids]
        out[ref] = {"missing": len(mine), "first": mine[:10]}
    return out


REMINDER = ("{doc} is the durable copy: commit it in its data repository (git add, git commit) or back it up. A copy "
            "opened in the graph explorer, a browser or a chat lives only there.")


# ------------------------------------------------------------------------------------------ tables
def _command(source: str, into: str, opts: dict, extra: list[str]) -> str:
    parts = ["scale", "add-source", source, "--into", into]
    for k in ("kind", "sheet", "header_row", "range", "template", "origin", "recorded_at", "namespace", "from_source",
              "locator", "data_rights"):
        if opts.get(k) is not None and not any(x.startswith(f"--{k.replace('_', '-')} ") for x in extra):
            parts += [f"--{k.replace('_', '-')}", str(opts[k])]
    for role, col in (opts.get("bind") or {}).items():
        if not any(x.startswith(f"--bind {role}=") for x in extra):
            parts += ["--bind", f"{role}={col}"]
    for c in opts.get("confirm") or []:
        parts += ["--confirm", c]
    return " ".join(shlex.quote(x) for x in parts) + "".join(f" {x}" for x in extra)


def _questions(best: dict, res: dict, opts: dict, header: list[str]) -> tuple[list[dict], list[str]]:
    """The uncertain items of the best match a person settles (questions, each with the option and the tool argument
    that settles it), and the ones that are only notes."""
    questions: list[dict] = []
    notes: list[str] = []
    confirmed = set(opts.get("confirm") or [])
    seen: set[str] = set()

    def ask(qid: str, about: str, why: str, cli: list[str], tool: dict, candidates=None) -> None:
        if qid in seen:
            return
        seen.add(qid)
        questions.append({"id": qid, "about": about, "why": why, "settle": {"options": cli, "tool_arguments": tool},
                          **({"candidates": candidates} if candidates else {})})

    others = [c for c in res["candidates"][1:] if c.get("sheet") != best.get("sheet") and c["complete"]]
    for u in best["uncertain"]:
        role, col, why = u.get("role"), u.get("column"), u["why"]
        cands = [c["column"] for c in u.get("candidates") or []]
        if role is None and why.startswith("sheet "):
            if opts.get("sheet") is None and others:
                sheets = [best["sheet"], *[c["sheet"] for c in others]]
                ask("sheet", "which sheet", f"more than one sheet fits a built-in mapping ({', '.join(map(repr, sheets))})",
                    [f"--sheet {shlex.quote(str(s))}" for s in sheets], {"sheet": best["sheet"]}, sheets)
            else:
                notes.append(why)
        elif role is None and NOTE.match(why):
            notes.append(why.replace("give input.range", "give --range (an A1 range)"))
        elif u.get("blocking"):
            where = col or role or "the file"
            ask(f"fix:{where}", f"cells of {where}", why, [], {},
                u.get("tokens"))
        elif role == "recorded_at" and col is None:
            ask("recorded_at", "when the data were recorded", why,
                ["--recorded-at YYYY-MM-DDThh:mm:ssZ", "--origin reported", "--bind recorded_at=COLUMN"],
                {"recorded_at": "YYYY-MM-DDThh:mm:ssZ"})
        elif "assumed" in why or "bound by --bind" in why:
            qid = f"unit:{col}"
            if qid not in confirmed:
                ask(qid, f"the unit of {col!r}", why + "; if it is another unit, write it in the column's header "
                    "(Name (unit)) and add the file again", [f"--confirm {shlex.quote(qid)}"], {"confirm": [qid]})
        elif role is not None:
            options = [f"--bind {shlex.quote(f'{role}={c}')}" for c in (cands or [])] or \
                      [f"--bind {role}=COLUMN"]
            ask(f"bind:{role}", f"which column is {role}", why + (f" (now {col!r})" if col else ""), options,
                {"bind": {role: cands[0] if cands else "COLUMN"}}, cands or header)
        else:
            notes.append(why)
    if not best["complete"] and not questions:
        ask("mapping", "the mapping", "the mapping is not complete: " + "; ".join(best["problems"] or ["?CHOOSE left"]),
            ["--template ID   # another built-in mapping: scale data match --list"], {})
    # another kind of table that fits as well: which one it is, the person says
    rivals = [c for c in res["candidates"][1:] if c["template"] != best["template"] and c.get("sheet") == best.get("sheet")
              and c["complete"] and c["score"] >= best["score"] - datamatch.AMBIGUITY]
    if rivals and opts.get("template") is None:
        kinds = [best["template"], *[c["template"] for c in rivals]]
        ask("template", "which kind of table", "more than one built-in mapping fits it as well ("
            + ", ".join(f"{c['template']} {c['score']}" for c in [best, *rivals]) + ")",
            [f"--template {t}" for t in kinds], {"template": best["template"]}, kinds)
    # what the mapping assumes about the experiment (a control mode, a normalization, what a blank means)
    if best["confirm"]:
        qid = f"assumptions:{best['template']}"
        if qid not in confirmed:
            ask(qid, "what the mapping assumes", "the mapping records these as stated for every row: "
                + "; ".join(best["confirm"]) + ". Confirm them, or say what differs (then the file, or a mapping of "
                "your own, states it: docs/data.md)", [f"--confirm {qid}"], {"confirm": [qid]}, best["confirm"])
    return questions, notes


def _raw_target(src: Path, data: bytes, dest: dict) -> Path | None:
    """Inside a data repository, where a file from outside it is copied (raw/, git-ignored) so that the Dataset's uri
    keeps pointing at the bytes: its name, or its name with the bytes' digest when another file holds the name. None:
    no copy (no data repository, or the file is already in it)."""
    repo = dest["data_repository"]
    if repo is None or repo.resolve() in src.resolve().parents:
        return None
    raw = _safe(repo / "raw", dest)
    target = raw / src.name
    if target.exists() and target.read_bytes() != data:
        from .datatable import sha256
        target = raw / f"{src.stem}-{sha256(data)[:12]}{src.suffix}"
        if target.exists() and target.read_bytes() != data:
            _fail(f"{target} exists with other bytes than {src}; move it aside first")
    return _safe(target, dest)


def _copy_raw(src_data: bytes, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(src_data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _link_source(doc: dict, dataset_id: str, source_id: str, locator: str | None) -> list[str]:
    """The Dataset cites the Source it belongs to (a curation note) and is REPORTED_IN it; returns what was added."""
    nodes = {n["id"]: n for n in doc["nodes"]}
    src = nodes.get(source_id)
    if src is None or src.get("label") != "Source":
        _fail(f"--from-source {source_id}: the destination holds no Source of that id; add the publication first "
              "(scale add-source <its PDF or DOI> --into <doc>, then --finish)")
    ds = nodes[dataset_id]
    ev = {"source": source_id, "locator": locator or "the publication's data", "kind": "curation_note",
          "note": "The person who added this data file stated that it holds the data of this publication."}
    added = []
    evs = list(ds["props"].get("evidence") or [])
    if not any(e.get("source") == source_id for e in evs):
        ds["props"]["evidence"] = [*evs, ev]
        added.append(f"{dataset_id} evidence -> {source_id}")
    have = {e["id"] for e in doc["links"]}
    for link in pgraph.derive_reported_in({"nodes": [ds]}):
        if link["id"] not in have:
            doc["links"].append(link)
            added.append(link["id"])
    return added


def _table(source: str, det: dict, dest: dict, doc: dict, spec: dict, repo: Path, opts: dict) -> dict:
    path = Path(det["path"])
    _source_audience(path, dest, doc)
    if opts.get("from_source") and not any(n.get("id") == opts["from_source"] and n.get("label") == "Source"
                                           for n in doc["nodes"]):
        _fail(f"--from-source {opts['from_source']}: the destination holds no Source of that id; add the publication "
              "first (scale add-source <its PDF or DOI> --into <doc>, then --finish)")
    data = path.read_bytes()
    st = (doc.get("graph") or {}).get("stewardship") or {}
    ns = opts.get("namespace")
    if ns is None:
        pre = str(st.get("project") or "").split(":", 1)[0].lower()
        ns = pre if re.fullmatch(r"[a-z][a-z0-9_-]*", pre or "") else "lab"
    if opts.get("recorded_at") is not None and not DATETIME.fullmatch(opts["recorded_at"]):
        _fail("--recorded-at is an ISO 8601 date-time with a time zone (2026-03-02T10:00:00Z)")
    res = datamatch.match(data, path.name, datamatch.load_templates(repo), spec, sheet=opts.get("sheet"),
                          header_row=opts.get("header_row"), template=opts.get("template"), bind=opts.get("bind") or {},
                          namespace=ns, origin=opts.get("origin"), recorded_at=opts.get("recorded_at"), top=5,
                          options={"range": opts["range"]} if opts.get("range") else None)
    into = str(dest["path"])
    reading = [{k: r[k] for k in ("sheet", "header", "rows", "error") if k in r} for r in res["reading"]]
    if not res["candidates"]:
        return {"route": "table", "status": "questions", "into": into, "written": None, "reading": reading,
                "questions": [{"id": "mapping", "about": "the kind of table",
                               "why": "no built-in mapping fits this file (scale data match --list shows them)",
                               "settle": {"options": ["--sheet NAME", "--header-row N",
                                                      "--template ID   # scale data match --list"],
                                          "tool_arguments": {}}}],
                "retry": _command(source, into, opts, []),
                "next": ["or write a mapping for this layout (docs/data.md, The mapping) and run scale data add"]}
    best = res["candidates"][0]
    header = next((r["header"] for r in res["reading"] if r.get("sheet") == best.get("sheet")), [])
    questions, notes = _questions(best, res, opts, header)
    if questions and opts.get("data_rights") is None:           # asked with the others, not in a second round
        try:
            origin0 = load_mapping(best["mapping_yaml"].encode("utf-8"))["records"][0]["origin"]
        except ScaleError:
            origin0 = opts.get("origin")
        if origin0 == "reported" or opts.get("from_source"):
            questions.append(_RIGHTS_Q)
    out = {"route": "table", "into": into, "template": best["template"], "title": best["title"],
           "score": best["score"], "sheet": best.get("sheet"),
           "bindings": {r: b["column"] for r, b in best["bindings"].items()},
           "melted": {r: [c["column"] for c in cs] for r, cs in best["melt"].items()},
           "unmapped_columns": best["unmapped_columns"], "assumptions": best["confirm"], "notes": notes,
           # what a person confirming the reading looks at (the graph explorer's mapping view): each role's column and
           # unit, each wide column's unit, the header read and the other kinds of table that fit
           "roles": {r: {k: b[k] for k in ("column", "unit", "unit_from_header", "score", "by") if k in b}
                     for r, b in best["bindings"].items()},
           "melt_columns": {r: [{k: c[k] for k in ("column", "unit", "id", "target", "unresolved_target") if k in c}
                                for c in cs] for r, cs in best["melt"].items()},
           "header": header, "reading": res["reading"],
           "alternatives": [{k: c.get(k) for k in ("template", "title", "score", "sheet", "complete")}
                            for c in res["candidates"][1:]]}
    if questions:
        extra = [q["settle"]["options"][0] for q in questions if q["settle"]["options"]]
        tool = {"source": source, "into": into, **{k: v for k, v in opts.items() if v not in (None, [], {})}}
        for q in questions:
            for k, v in q["settle"]["tool_arguments"].items():
                tool[k] = {**tool.get(k, {}), **v} if isinstance(v, dict) else \
                    [*tool.get(k, []), *v] if isinstance(v, list) else v
        return {**out, "status": "questions", "written": None, "questions": questions,
                "retry": _command(source, into, opts, extra), "retry_tool": {"tool": "scale_add_source",
                                                                            "arguments": tool},
                "next": ["answer each question (the person who knows the data, never a guess), then run the retry "
                         "command with the answers in place of the suggested values"]}
    target = _raw_target(path, data, dest)
    file_path = target or path
    try:
        uri = Path(os.path.relpath(file_path, dest["path"].parent)).as_posix()
    except ValueError:
        uri = file_path.name
    p = plan(data, best["mapping_yaml"].encode("utf-8"), doc, spec, path.name, uri=uri)
    new = p.pop("document")
    dataset = p.get("input", {}).get("file_dataset") or p["dataset"]
    # the cells a value cites are quoted from the Dataset, so the Dataset is a source with rights (the copyright cap)
    rf = _rights_file(dest)
    held = set(((load_yaml(rf) or {}).get("sources") or {}) if rf.is_file() else {})
    need = [x for x in p["added"] if x in (p["dataset"], p.get("input", {}).get("file_dataset")) and x not in held]
    origin = load_mapping(best["mapping_yaml"].encode("utf-8"))["records"][0]["origin"]   # the records' (the mapping's)
    data_rights = opts.get("data_rights")
    if data_rights is not None and data_rights not in DATA_RIGHTS:
        _fail(f"--data-rights is one of {', '.join(DATA_RIGHTS)}")
    if need and data_rights is None and (origin == "reported" or opts.get("from_source")):
        q = _RIGHTS_Q
        return {**out, "status": "questions", "written": None, "questions": [q],
                "retry": _command(source, into, opts, ["--data-rights CLASS"]),
                "retry_tool": {"tool": "scale_add_source", "arguments": {
                    "source": source, "into": into, **{k: v for k, v in opts.items() if v not in (None, [], {})},
                    "data_rights": "CLASS"}},
                "next": ["say what the publication's licence is (the person who knows it, never a guess)"]}
    entries = {x: _data_rights(data_rights, origin, path.name) for x in need}
    linked: list[str] = []
    if opts.get("from_source"):
        linked = _link_source(new, dataset, opts["from_source"], opts.get("locator"))
        fresh, _ = _new_errors(doc, new, spec)
        if fresh:
            _fail("linking the data to the publication would make the document invalid: "
                  + "; ".join(f"{e['record']}: {e['message']}" for e in fresh[:10]))
    _audience(dest, new)
    rights_plan = _rights_plan(dest, entries)               # a conflicting entry refuses before anything is written
    _cap(dest, new, rights_plan, set(entries) | {p["dataset"], dataset})
    # every cited cell is checked against the file before anything is written
    from .datatable import verify
    v = verify(new, files={dataset: path}, base=dest["path"].parent)
    mine = [x for x in v["datasets"] if x["dataset"] in (p["dataset"], dataset)]
    vstatus = "failed" if any(x["status"] == "failed" for x in mine) else \
        "verified" if mine and all(x["status"] == "verified" for x in mine) else "unverified"
    if vstatus == "failed":
        _fail(f"the cells the records would cite do not hold their values in {path} ("
              + "; ".join(pr for x in mine for pr in x.get("problems") or [])[:500] + "); nothing was written")
    rep = pgraph.validate(new, spec)
    ids = {n["id"] for n in p["nodes"]}
    out.update({"dataset": p["dataset"], "file_dataset": p.get("input", {}).get("file_dataset"),
                "records": p["records"], "results": p["results"], "added": len(p["added"]) + len(linked),
                "unchanged": len(p["unchanged"]), "created": p.get("created", []),
                "linked_to_source": linked, "validation": p["validation"], "warnings": p["warnings"][:20],
                "checklist": _checklist(rep, ids), "raw_copy": str(target) if target else None,
                "verify": {"status": vstatus, "datasets": mine}})
    if opts.get("dry_run"):
        return {**out, "status": "dry_run", "written": None, "would_add": p["added"] + linked,
                "rights_entries": entries,
                "next": [_command(source, into, {**opts, "dry_run": None}, []) + "   # to write it"]}
    written = None
    mpath = _safe(_work(dest) / "mappings", dest)
    mpath.mkdir(parents=True, exist_ok=True)
    mpath = _safe(mpath / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', path.stem)}.{best['template']}.map.yaml", dest)
    if p["added"] or linked:
        if target is not None:
            _copy_raw(data, target)
        datamatch.save(mpath, best["mapping_yaml"])
        out["rights"] = _rights_commit(rights_plan)
        write_atomic(dest["path"], new)
        written = into
    return {**out, "status": "added" if written else "unchanged", "written": written,
            "mapping": str(mpath) if written else None,
            "next": [f"scale graph validate {into}", f"scale data verify {into}",
                     "open the document in the graph explorer (Open a graph or bundle) to look at it",
                     REMINDER.format(doc=into)]}


# ------------------------------------------------------------------------------------------ documents
def _cached(get):
    memo: dict = {}

    def g(url: str, headers: dict) -> bytes:
        if url not in memo:
            memo[url] = get(url, headers)
        return memo[url]
    return g


def _identity(path: Path | None, doi: str | None) -> dict:
    """What a curation folder was started from: the DOI and the digest of the file, so that another file of the same
    name never reuses it."""
    from .datatable import sha256
    return {"doi": doi, "sha256": sha256(path.read_bytes()) if path else None}


def _document(source: str, det: dict, dest: dict, doc: dict, repo: Path, opts: dict, get) -> dict:
    into = str(dest["path"])
    path = Path(det["path"]) if det.get("path") else None
    if path is not None:
        _source_audience(path, dest, doc)
    doi = det.get("doi") or opts.get("doi")
    if doi:
        doi = curate.normalize_doi(doi)
    if opts.get("source_kind") is not None and opts["source_kind"] not in SOURCE_KINDS:
        _fail(f"--source-kind is one of {', '.join(SOURCE_KINDS)}")
    slug = curate.slug_of(doi, path)
    ident = _identity(path, doi)
    sources = _safe(_work(dest) / "sources", dest)
    folder = _safe(sources / slug, dest)
    marker = "added-from.json"
    if (folder / "source.json").is_file():
        same = (folder / marker).is_file() and load_doc(folder / marker) == ident
        if same:
            return {"route": "document", "status": "started", "into": into, "folder": str(folder), "written": None,
                    "already": True, "next": _curation_steps(folder, slug, into)}
        slug = f"{slug}-{(ident['sha256'] or curate.normalize_doi(doi).replace('/', '-'))[:8]}"
        folder = _safe(sources / slug, dest)        # another source of the same name: a folder of its own
        if (folder / "source.json").is_file():
            return {"route": "document", "status": "started", "into": into, "folder": str(folder), "written": None,
                    "already": True, "next": _curation_steps(folder, slug, into)}
    nxt = _curation_steps(folder, slug, into)
    get = _cached(get)
    if path is None:                                  # a DOI alone: curate only from a copy under an open licence
        email = opts.get("email") or os.environ.get(curate.EMAIL_ENV)
        meta = curate.resolve_doi(doi, email, get)
        oa = meta.get("open_access")
        if not oa or curate.rights_class([oa.get("licence") or ""]) == "in_copyright":
            return {"route": "document", "status": "questions", "into": into, "written": None,
                    "source": {k: meta.get(k) for k in ("doi", "title", "year", "venue", "kind")},
                    "notes": meta.get("notes", []),
                    "questions": [{"id": "file", "about": "the publication's text",
                                   "why": "no open-access copy under an open licence was found"
                                          + ("" if email else " (no contact address: give --email to look for one)")
                                          + "; SCALE reads the publication from its PDF or text",
                                   "settle": {"options": [f"scale add-source PDF_FILE --doi {doi} --into {into}",
                                                          "--email ADDRESS"],
                                              "tool_arguments": {"source": "PDF_FILE", "doi": doi}}}],
                    "retry": f"scale add-source PDF_FILE --doi {doi} --into {shlex.quote(into)}"}
    st = (doc.get("graph") or {}).get("stewardship") or {}
    if opts.get("dry_run"):
        return {"route": "document", "status": "dry_run", "into": into, "folder": str(folder), "written": None,
                "stewardship": st, "next": nxt}
    by = st.get("contributed_by") or []
    res = curate.start(folder, doi=doi, pdf=path if det["format"] == "pdf" else None,
                       text=path if det["format"] in ("text", "csv") else None, email=opts.get("email"),
                       version=(doc.get("graph") or {}).get("schema_version"), slug=slug,
                       contributor=by[0] if by else None, project=st.get("project"), repo=repo, get=get,
                       stewardship=dict(st), source_kind=opts.get("source_kind"))
    from .util import write_canonical
    write_canonical(folder / marker, ident)
    # the records DOC's header names are DOC's: the folder holds the same copies, so finishing never conflicts
    gpath = folder / res["graph"]
    g = load_doc(gpath)
    mine = {n["id"]: n for n in doc["nodes"]}
    g["nodes"] = [mine.get(n["id"], n) if n.get("label") in ("Project", "Institution") else n for n in g["nodes"]]
    links = {e["id"]: e for e in doc["links"]}
    g["links"] = [links.get(e["id"], e) for e in g["links"]]
    write_atomic(gpath, g)
    return {"route": "document", "status": "started", "into": into, "folder": str(folder), "graph": str(gpath),
            "source_id": res["source_id"], "rights_class": res["rights_class"], "pages": res["pages"],
            "page_texts": str(folder / res["page_texts"]) if res["page_texts"] else None, "notes": res["notes"],
            "stewardship": res["stewardship"], "written": None, "next": nxt}


def _curation_steps(folder: Path, slug: str, into: str) -> list[str]:
    g = folder / f"{slug}.graph.json"
    return [f"read {folder / 'BRIEF.md'} and follow the curate-corpus skill (scale call skills "
            "'{\"name\": \"curate-corpus\"}', or /curate-corpus in Claude Code) to fill "
            f"{g} from {folder / 'work/texts'}: what the source reports, each record with its evidence",
            "agent tools: scale_describe (the labels and the checklist), scale_source_check (after each batch of "
            "records), scale_validate; never invent a property or a value",
            f"scale source check {folder} --derive-links   # until it reports no error",
            f"scale review values {g} --out {folder / 'review-values.md'}   # the person checks every flagged value",
            f"scale add-source --finish {folder} --into {into}   # folds the checked graph into the document"]


# ------------------------------------------------------------------------------------------ merging a graph
def _rebase(r: dict, src_dir: Path | None, dest_dir: Path) -> dict:
    """A Dataset's local uri (a path relative to its document) made relative to the destination, so that it still
    names the same file."""
    props = r.get("props")
    uri = props.get("uri") if r.get("label") == "Dataset" and isinstance(props, dict) else None
    if src_dir is None or not isinstance(uri, str) or not uri or re.match(r"^[a-z][a-z0-9+.-]*:", uri, re.I) \
            or Path(uri).is_absolute():
        return r
    try:
        new = Path(os.path.relpath((src_dir / uri).resolve(), dest_dir)).as_posix()
    except ValueError:
        new = str((src_dir / uri).resolve())
    return r if new == uri else {**r, "props": {**props, "uri": new}}


def _merge(doc: dict, incoming: dict, src_dir: Path | None = None, dest_dir: Path | None = None
           ) -> tuple[dict, list[str], list[str], list[str]]:
    """``incoming``'s records added to ``doc``: each as it would be written (its audience kept: written on its own
    stewardship where ``doc``'s header would give it another; a Dataset's local uri rebased); one ``doc`` holds alike
    is unchanged, the Project and Institution records ``doc``'s header names stay ``doc``'s, any other record ``doc``
    holds with other content is a conflict."""
    out = copy.deepcopy(doc)
    st = pgraph.stewardship_of((doc.get("graph") or {}).get("stewardship"))
    keep = {st.get("project"), *(st.get("contributed_by") or [])}
    have = {r["id"]: r for r in [*out["nodes"], *out["links"]]}
    added, unchanged, conflicts = [], [], []
    for kind in ("nodes", "links"):
        for r in incoming.get(kind) or []:
            w = pgraph._own_audience(incoming, copy.deepcopy(r), st)
            if dest_dir is not None:
                w = _rebase(w, src_dir, dest_dir)
            cur = have.get(r.get("id"))
            if cur is not None:
                same = any(json.dumps(cur, sort_keys=True) == json.dumps(x, sort_keys=True) for x in (w, r))
                ours = (r["id"] in keep and cur.get("label") == r.get("label")) or \
                    (kind == "links" and r.get("source") in keep and r.get("target") in keep
                     and (cur.get("type"), cur.get("source"), cur.get("target"))
                     == (r.get("type"), r.get("source"), r.get("target")))
                if same or ours:
                    unchanged.append(r["id"])
                else:
                    conflicts.append(r["id"])
                continue
            out[kind].append(w)
            added.append(r["id"])
    return out, added, unchanged, conflicts


def _fold(dest: dict, doc: dict, incoming: dict, repo: Path, what: str, dry_run: bool, entries: dict,
          src_dir: Path | None = None) -> dict:
    """Merge ``incoming`` into the destination with the rights ``entries`` of the sources it quotes: every check
    (version, conflicts, validity, audience, rights, the copyright cap) before anything is written."""
    spec = _spec(repo, doc)
    if (incoming.get("graph") or {}).get("schema_version") != (doc.get("graph") or {}).get("schema_version"):
        _fail(f"{what} pins scale-catal {(incoming.get('graph') or {}).get('schema_version')} and the destination "
              f"{(doc.get('graph') or {}).get('schema_version')}: move one to the other's version first "
              "(scale graph migrate, or scale graph upgrade)")
    out, added, unchanged, conflicts = _merge(doc, incoming, src_dir, dest["path"].parent)
    if conflicts:
        _fail(f"{len(conflicts)} records of {what} are in the destination with other content "
              f"({', '.join(conflicts[:8])}{' ...' if len(conflicts) > 8 else ''}); nothing was merged: a merge never "
              "overwrites a record. Give the incoming records other ids, or remove the destination's first")
    fresh, rep = _new_errors(doc, out, spec)
    if fresh:
        _fail(f"merging {what} would make the destination invalid:\n"
              + "\n".join(f"  {e['record']}: {e['message']}" for e in fresh[:20]))
    _audience(dest, out)
    rights_plan = _rights_plan(dest, entries)
    _cap(dest, out, rights_plan, set(entries))
    res = {"into": str(dest["path"]), "added": len(added), "unchanged": len(unchanged), "added_ids": added[:50],
           "validation": {"valid": rep.ok, "errors": len(rep.errors), "warnings": len(rep.warnings)},
           "checklist": _checklist(rep, set(added))}
    if dry_run:
        return {**res, "status": "dry_run", "written": None, "rights": rights_plan["summary"]}
    rights = _rights_commit(rights_plan)          # first: an extra rights entry is harmless, a graph without its own is not
    if added:
        write_atomic(dest["path"], out)
    return {**res, "status": "merged" if added else "unchanged", "written": str(dest["path"]) if added else None,
            "rights": rights}


def _rights_file(dest: dict) -> Path:
    """The rights file the destination's quotes are read against: in a data repository, the nearest rights.yaml at or
    above the document within it (else the repository's own); elsewhere, the one beside the document."""
    top = dest["data_repository"]
    if top is not None:
        for d in [dest["path"].parent, *dest["path"].parent.parents]:
            if (d / "rights.yaml").is_file():
                return _safe(d / "rights.yaml", dest)
            if d == top:
                break
        return _safe(top / "rights.yaml", dest)
    return _safe(dest["path"].parent / "rights.yaml", dest)


def _rights_plan(dest: dict, entries: dict[str, dict]) -> dict:
    """The rights file with ``entries`` added, checked (a malformed file, or an entry that differs from the file's,
    refuses), nothing written: :func:`_rights_commit` writes it."""
    from . import rights
    rf = _rights_file(dest)
    data = load_yaml(rf) if rf.is_file() else {}
    data = data if isinstance(data, dict) else {}
    if rf.is_file():
        rights.load_rights(rf)
    have = dict(data.get("sources") or {})
    new, same = {}, []
    for sid, e in entries.items():
        if sid in have:
            if have[sid] != e:
                _fail(f"{rf}: {sid} has another rights entry ({have[sid]}) than the one this source brings ({e}); "
                      "settle which is right and make them the same first; nothing was written")
            same.append(sid)
        else:
            new[sid] = e
    merged = {**data, "sources": {**have, **new}}
    rights.parse_rights(merged, rf)
    return {"file": rf, "data": merged if new else None,
            "summary": {"file": str(rf), "added": sorted(new), "unchanged": same}}


def _rights_commit(plan_: dict) -> dict:
    if plan_["data"] is not None:
        import tempfile
        rf = plan_["file"]
        fd, tmp = tempfile.mkstemp(prefix=f".{rf.name}.", dir=rf.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("# The rights of the sources the documents quote (scale evidence report; scale add-source "
                        "adds the entries of what it adds).\n" + dump_yaml(plan_["data"]))
            os.replace(tmp, rf)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return plan_["summary"]


def _cap(dest: dict, doc: dict, rights_plan: dict, ids: set[str]) -> None:
    """The copyright cap of the destination (unless its data repository sets copyright_cap: false), for the sources
    ``ids`` this addition quotes, under the rights file as it will be."""
    from . import rights
    if dest["policy"] is not None and not dest["policy"]["copyright_cap"]:
        return
    data = rights_plan["data"] or (load_yaml(rights_plan["file"]) if rights_plan["file"].is_file() else None) or \
        {"sources": {}}
    rep = rights.report(doc, rights.parse_rights(data, rights_plan["file"]))
    bad = [v for v in rep["violations"] if any(v.startswith(f"{i}:") for i in ids)]
    if bad:
        _fail("the quotes would exceed the copyright cap (" + "; ".join(bad[:5]) + "); nothing was written")


def finish(folder: str | Path, into: str | Path, repo: Path | None = None, dry_run: bool = False) -> dict:
    """Fold a curation folder's checked graph into ``into``: the folder must check without error and within the
    copyright cap; records keep their ids; the Source's rights entry goes to the destination's rights file."""
    repo = Path(repo) if repo else find_repo_root()
    dest = destination(into)
    d, info, gpath = curate._folder(Path(folder))
    chk = curate.check(d, repo)
    if not chk["ok"]:
        _fail(f"{gpath} has {len(chk['errors'])} errors (first: "
              + "; ".join(f"{e['record']}: {e['message']}" for e in chk["errors"][:5])
              + f"); fix them first: scale source check {d}")
    cap_applies = dest["policy"] is None or dest["policy"]["copyright_cap"]
    if cap_applies and not chk["cap"]["ok"]:
        _fail(f"{gpath} quotes beyond the copyright cap ({chk['cap']['violations'][:3]}); shorten the quotes first")
    doc = _load_dest(dest)
    g = load_doc(gpath)
    from . import rights
    entries = {k: _plain(v) for k, v in rights.load_rights(d / "rights.yaml").items()
               if any(n["id"] == k for n in g["nodes"])}
    res = _fold(dest, doc, g, repo, str(gpath), dry_run, entries, gpath.parent)
    res.update(route="document", folder=str(d), source_id=info.get("source_id"),
               conformance=chk.get("conformance"), values_flagged=chk["values"].get("flag", 0),
               next=[f"scale graph validate {dest['path']}", REMINDER.format(doc=dest["path"])])
    return res


def _plain(entry: dict) -> dict:
    """A rights entry as its file writes it (the keys load_rights reads)."""
    return {k: v for k, v in entry.items() if v is not None}


def _cited(g: dict) -> set[str]:
    """The sources a graph quotes: every Source and every Dataset its evidence cites."""
    out = {n["id"] for n in g["nodes"] if n.get("label") == "Source"}
    for r in [*g["nodes"], *g["links"]]:
        for ev in (r.get("props") or {}).get("evidence") or []:
            if isinstance(ev, dict) and isinstance(ev.get("source"), str):
                out.add(ev["source"])
    return out


def _graph(source: str, det: dict, dest: dict, doc: dict, repo: Path, opts: dict) -> dict:
    p = Path(det["path"])
    try:
        g = load_doc(p)
    except Exception as exc:                          # noqa: BLE001
        raise ScaleError(f"{p}: not a readable JSON document ({exc})") from None
    if not isinstance(g, dict) or not isinstance(g.get("nodes"), list) or not isinstance(g.get("links"), list):
        _fail(f"{p}: a JSON file that is no scale-catal document (node-link JSON with nodes and links)")
    spec = pgraph.load_spec(pgraph.spec_for(repo, (g.get("graph") or {}).get("schema_version"), None, g))
    rep = pgraph.validate(g, spec)
    base = {"route": "graph", "into": str(dest["path"]), "schema_version": spec["version"],
            "nodes": len(g["nodes"]), "links": len(g["links"]),
            "validation": {"valid": rep.ok, "errors": len(rep.errors), "warnings": len(rep.warnings),
                           "first_errors": rep.errors[:10]}}
    if not rep.ok:
        return {**base, "status": "invalid", "written": None,
                "next": [f"scale graph validate {p}   # fix what it reports, then add it again"]}
    if not opts.get("merge"):
        return {**base, "status": "validated", "written": None,
                "next": [_command(source, str(dest["path"]), opts, ["--merge"]) + "   # to merge it into the document",
                         f"or query it beside the document without merging: scale graph query --graph a={dest['path']} "
                         f"--graph b={p} PLAN"]}
    from . import rights
    srcs = _cited(g)
    rf = rights.rights_file(p)
    entries = {k: _plain(v) for k, v in rights.load_rights(rf).items() if k in srcs} if rf else {}
    res = _fold(dest, doc, g, repo, str(p), opts.get("dry_run", False), entries, p.parent)
    have = set(((load_yaml(_rights_file(dest)) or {}).get("sources") or {}) if _rights_file(dest).is_file() else ())
    missing = sorted(srcs - set(entries) - have)
    if missing:
        res["warnings_rights"] = (f"{len(missing)} sources the graph quotes have no rights entry in "
                                  f"{_rights_file(dest)} ({', '.join(missing[:5])}): add one (class, licence, basis) "
                                  "before committing")
    return {**base, **res, "next": [f"scale graph validate {dest['path']}", REMINDER.format(doc=dest["path"])]}


def _load_dest(dest: dict) -> dict:
    try:
        doc = load_doc(dest["path"])
    except Exception as exc:                          # noqa: BLE001
        raise ScaleError(f"{dest['path']}: not a readable scale-catal document ({exc})") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list) or not isinstance(doc.get("links"), list):
        _fail(f"{dest['path']}: not a scale-catal document (node-link JSON with nodes and links)")
    return doc


# ------------------------------------------------------------------------------------------ the entry point
def add(source: str | None, into: str | Path, repo: Path | None = None, finish_folder: str | Path | None = None,
        get=None, **opts) -> dict:
    """Route ``source`` (a file path or a DOI) into the document ``into`` (see the module docstring). ``opts``: kind,
    dry_run, sheet, header_row, recorded_at, origin, bind, confirm, template, namespace, from_source, locator, doi,
    email, source_kind, merge."""
    repo = Path(repo) if repo else find_repo_root()
    if finish_folder is not None:
        if source:
            _fail("--finish FOLDER takes no source: it folds a curation folder into the document")
        return finish(finish_folder, into, repo, dry_run=bool(opts.get("dry_run")))
    if not source:
        _fail("give a file (a PDF, a CSV or .xlsx table, a JSON graph, a text file) or a DOI, or --finish FOLDER")
    det = detect(str(source), opts.get("kind"))
    dest = destination(into)
    doc = _load_dest(dest)
    if det["route"] == "table":
        if opts.get("merge") or opts.get("doi"):
            _fail("--merge is for a graph and --doi for a document; this is a table")
        res = _table(str(source), det, dest, doc, _spec(repo, doc), repo, opts)
    elif det["route"] == "document":
        res = _document(str(source), det, dest, doc, repo, opts, get or curate.fetch)
    else:
        res = _graph(str(source), det, dest, doc, repo, opts)
    res["detected"] = det
    res["data_repository"] = str(dest["data_repository"]) if dest["data_repository"] else None
    return res


def text_report(res: dict) -> str:
    """The result for a person: what was added, what is asked, what the checklist says is missing, where it is saved."""
    lines = []
    det = res.get("detected") or {}
    what = det.get("doi") or Path(det.get("path", res.get("folder", ""))).name
    lines.append(f"{what}: {res.get('route')} -> {res.get('into')}: {res['status'].upper()}")
    if res.get("template"):
        lines.append(f"  read as {res['template']} ({res.get('title')}), score {res.get('score')}"
                     + (f", sheet {res['sheet']!r}" if res.get("sheet") else ""))
        for role, col in (res.get("bindings") or {}).items():
            lines.append(f"    {role} <- {col!r}")
        for role, cols in (res.get("melted") or {}).items():
            lines.append(f"    {role} <- {', '.join(map(repr, cols))}")
    for q in res.get("questions") or []:
        lines.append(f"  QUESTION {q['id']}: {q['about']}: {q['why']}")
        for o in q["settle"]["options"]:
            lines.append(f"      {o}")
    if res.get("retry"):
        lines.append(f"  then: {res['retry']}")
    for n in res.get("notes") or []:
        lines.append(f"  note: {n}")
    if not any(q["id"].startswith("assumptions:") for q in res.get("questions") or []):
        for a in res.get("assumptions") or []:
            lines.append(f"  assumed (confirmed): {a}")
    if res.get("unmapped_columns"):
        lines.append(f"  not read: {', '.join(res['unmapped_columns'])}")
    if res.get("records") is not None:
        lines.append(f"  {res['records']} records, {res['results']} results, {res['added']} records and links added, "
                     f"{res['unchanged']} already there (dataset {res['dataset']})")
    elif res.get("added") is not None:
        lines.append(f"  {res['added']} records and links added, {res.get('unchanged', 0)} already there")
    if res.get("raw_copy"):
        lines.append(f"  the file was copied to {res['raw_copy']} (kept on this computer; the Dataset points there)")
    for x in res.get("linked_to_source") or []:
        lines.append(f"  linked: {x}")
    v = res.get("validation")
    if v:
        lines.append(f"  structure: {'valid' if v.get('valid') else 'INVALID'} ({v.get('errors', 0)} errors)")
        for e in v.get("first_errors") or []:
            lines.append(f"    {e['record']}: {e['message']}")
    if res.get("verify"):
        lines.append(f"  cells checked against the file: {res['verify']['status'].upper()}")
    for ref, c in (res.get("checklist") or {}).items():
        lines.append(f"  checklist {ref}: {c['missing']} must items still missing on the added records")
        for x in c["first"][:5]:
            lines.append(f"    {x['record']}: {x['message']}")
        if c["missing"]:
            lines.append(f"    ({c['missing']} in all: what the file does not say, such as a test's reaction, feed or "
                         "electrolyte; add those records when you know them (scale graph describe CatalyticTest "
                         "--domain catalysis), never typical values)")
    if res.get("rights"):
        r = res["rights"]
        lines.append(f"  rights: {', '.join(r['added']) or 'nothing new'} in {r['file']}")
    if res.get("warnings_rights"):
        lines.append(f"  WARNING {res['warnings_rights']}")
    if res.get("folder") and res["route"] == "document" and res["status"] in ("started", "dry_run"):
        lines.append(f"  curation folder: {res['folder']}" + (" (already started)" if res.get("already") else ""))
        if res.get("source_id"):
            lines.append(f"  source id: {res['source_id']} (nothing of the source is in the document until --finish; "
                         "--from-source names it for its tables)")
    if res.get("mapping"):
        lines.append(f"  mapping saved: {res['mapping']}")
    for n in res.get("next") or []:
        lines.append(f"  next: {n}")
    return "\n".join(lines)


__all__ = ["add", "detect", "destination", "finish", "text_report", "ROUTES"]
