"""The graph explorer's private graphs: any source added in the reader's browser (site/worker.js runs this module in
Pyodide), by the same code as ``scale add-source`` (docs/data.md, "In the graph explorer").

A private graph is a folder (``ROOT/<id>/``) laid out as its download is, so that the command line reproduces it:

* ``graph.json``: the document (``scale data new``, then each source added by ``scale add-source``);
* ``rights.yaml``: the rights of the sources it quotes (written by ``scale add-source``);
* ``sources/``: the files added (a table, a graph; a publication's curated graph and its rights, never its PDF);
* ``work/mappings/``: the mapping each table was read with (``scale add-source`` saves it);
* ``manifest.json``: what made it: the tool and schema versions (the spec's sha256), each source's sha256 and how it
  was read, and the commands that rebuild ``graph.json`` from ``sources/`` in an empty folder.

Nothing here touches a network; nothing is written outside the folder. Every function returns JSON-ready data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
from contextlib import contextmanager
from pathlib import Path

from . import __version__, addsource, datamatch, pgraph
from .util import ScaleError, find_repo_root, load_doc, write_canonical

GRAPH = "graph.json"
MANIFEST = "manifest.json"
SOURCES = "sources"
MANIFEST_VERSION = 1
ACCESS = ("project", "institution", "consortium", "public")
#: the options of ``scale add-source`` a table step may carry (the rest are the router's own)
TABLE_OPTS = ("sheet", "header_row", "range", "recorded_at", "origin", "bind", "confirm", "template", "namespace", "data_rights",
              "from_source", "locator")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _fail(msg: str) -> None:
    raise ScaleError(msg)


def _root(base: str | Path, gid: str) -> Path:
    if not isinstance(gid, str) or not _ID.match(gid):
        _fail(f"{gid!r}: a private graph's id is 1-64 lower-case letters, digits or '-'")
    return Path(base) / gid


@contextmanager
def _inside(root: Path):
    """Run with the folder as the current directory, so that the router writes and reports relative paths."""
    was = os.getcwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(was)


def safe_name(name: str) -> str:
    """A file name as it is kept under sources/: its last part, letters, digits, '.', '_' and '-' only."""
    stem = Path(str(name).replace("\\", "/")).name
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-") or "file"
    return clean[:120]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(root: Path) -> dict:
    p = root / MANIFEST
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _command(parts: list[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in parts)


def _write_manifest(root: Path, man: dict, repo: Path) -> dict:
    doc = load_doc(root / GRAPH)
    spec = pgraph.load_spec(pgraph.spec_for(repo, (doc.get("graph") or {}).get("schema_version"), None, doc))
    data = (root / GRAPH).read_bytes()
    st = (doc.get("graph") or {}).get("stewardship") or {}
    man.update({
        "manifest_version": MANIFEST_VERSION,
        "made_by": "the SCALE graph explorer, in the reader's browser (nothing was uploaded)",
        "tool": f"scale-catal {__version__}",
        "schema": {"name": spec.get("schema", "scale-catal"), "version": spec["version"],
                   "spec_sha256": pgraph.spec_sha256(spec)},
        "graph": {"file": GRAPH, "sha256": sha256(data), "bytes": len(data), "title": (doc.get("graph") or {}).get("title"),
                  "access": st.get("access"), "nodes": len(doc.get("nodes") or []), "links": len(doc.get("links") or [])},
        "private": "This graph is the reader's own. The page never contributes it; it leaves this computer only as "
                   "the reader sends it.",
        "reproduce": {
            "where": "an empty folder outside the SCALE repository, with this download's sources/ copied into it",
            "commands": [s["command"] for s in man.get("steps") or [] if s.get("command")]
                        + [_command(["scale", "graph", "validate", GRAPH]), _command(["scale", "data", "verify", GRAPH])],
            "compare": f"the {GRAPH} it writes is this one, byte for byte (its sha256 above), when the tool and schema "
                       "versions are these",
        },
    })
    write_canonical(root / MANIFEST, man)
    return man


def new(base: str | Path, gid: str, project: str, contributor: str, access: str = "project", title: str | None = None,
        repo: Path | None = None) -> dict:
    """A new private graph: ``scale data new graph.json`` in its own folder (an existing folder is replaced)."""
    from .datarepo import new_document, refuse_framework
    repo = Path(repo) if repo else find_repo_root()
    if access not in ACCESS:
        _fail(f"access is one of {', '.join(ACCESS)}")
    root = _root(base, gid)
    refuse_framework(root, "a private graph")                 # before anything is made
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    with _inside(root):
        res = new_document(GRAPH, project=project, contributor=contributor, access=access, title=title, repo=repo)
    cmd = ["scale", "data", "new", GRAPH, "--project", project, "--contributor", contributor, "--access", access]
    if title:
        cmd += ["--title", title]
    man = _write_manifest(root, {"id": gid, "steps": [{"step": "new", "command": _command(cmd)}]}, repo)
    return {"id": gid, "stewardship": res["stewardship"], "schema_version": res["schema_version"], "manifest": man}


def detect(base: str | Path, name: str | None = None, data_b64: str | None = None, text: str | None = None) -> dict:
    """What a file (its name and bytes) or a DOI (``text``) is, as ``scale add-source`` reads it
    (:func:`scale.addsource.detect`): route table, document or graph, and the format. The file is read in a scratch
    folder and not kept."""
    if text is not None:                                 # a DOI never names a file here
        from .curate import normalize_doi
        return {"detected": {"route": "document", "doi": normalize_doi(str(text)), "format": "doi"}}
    scratch = Path(base) / ".inbox"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    data = base64.b64decode(data_b64 or "")
    p = scratch / safe_name(name or "file")
    p.write_bytes(data)
    try:
        det = addsource.detect(str(p))
    except ScaleError as exc:
        raise ScaleError(str(exc).replace(str(p.resolve()), str(name)).replace(str(p), str(name))) from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    det.pop("path", None)
    return {"detected": det, "name": name, "sha256": sha256(data), "bytes": len(data)}


def drop(base: str | Path, gid: str) -> dict:
    """Forget a private graph's folder (the reader removed it from the page)."""
    shutil.rmtree(_root(base, gid), ignore_errors=True)
    return {"id": gid, "dropped": True}


def stage(base: str | Path, gid: str, name: str, data_b64: str) -> dict:
    """Keep a file under the graph's sources/ (its name made safe; another file of that name gets the bytes' digest in
    its name) and say what it is, as ``scale add-source`` reads it (:func:`scale.addsource.detect`)."""
    root = _root(base, gid)
    if not (root / GRAPH).is_file():
        _fail(f"no private graph {gid}")
    data = base64.b64decode(data_b64)
    src = root / SOURCES
    src.mkdir(exist_ok=True)
    fname = safe_name(name)
    target = src / fname
    if target.exists() and target.read_bytes() != data:
        p = Path(fname)
        target = src / f"{p.stem}-{sha256(data)[:12]}{p.suffix}"
    if not target.exists():
        target.write_bytes(data)
    rel = target.relative_to(root).as_posix()
    with _inside(root):
        try:
            det = addsource.detect(rel)
        except ScaleError as exc:
            target.unlink(missing_ok=True)
            raise ScaleError(str(exc).replace(str(Path(rel).resolve()), name)) from None
    det["path"] = rel
    return {"file": rel, "name": name, "sha256": sha256(data), "bytes": len(data), "detected": det}


def unstage(base: str | Path, gid: str, file: str) -> dict:
    """Remove a file staged under sources/ that did not go in (skipped, refused): one no step of the manifest names
    stays out of the private graph's folder, its working copy and its download."""
    root = _root(base, gid)
    rel = str(file)
    if not rel.startswith(f"{SOURCES}/") or ".." in rel.split("/") or "\\" in rel:
        _fail(f"{file}: not a file under {SOURCES}/")
    named = {s.get("file") for s in _manifest(root).get("steps") or []}
    p = root / rel
    if rel in named or not p.is_file():
        return {"file": rel, "removed": False}
    p.unlink()
    return {"file": rel, "removed": True}


def _opts(opts: dict | None) -> dict:
    o = {k: v for k, v in (opts or {}).items() if k in TABLE_OPTS and v not in (None, "", [], {})}
    if "header_row" in o:
        o["header_row"] = int(o["header_row"])
    return o


def table(base: str | Path, gid: str, file: str, opts: dict | None = None, write: bool = False,
          repo: Path | None = None) -> dict:
    """Read a table under sources/ into the graph by ``scale add-source`` (its built-in mappings): with ``write`` false a
    dry run (what would be added, or the questions still open), else the addition, recorded in the manifest with the
    command that repeats it. The result carries the template's roles (for the confirmation view)."""
    repo = Path(repo) if repo else find_repo_root()
    root = _root(base, gid)
    o = _opts(opts)
    with _inside(root):
        res = addsource.add(file, GRAPH, repo=repo, kind="table", dry_run=not write, **o)
    if res.get("template"):
        t = next((x for x in datamatch.describe(datamatch.load_templates(repo)) if x["id"] == res["template"]), None)
        res["template_roles"] = (t or {}).get("roles") or {}
    res["library"] = [{"id": x["id"], "title": x["title"]} for x in datamatch.describe(datamatch.load_templates(repo))]
    res["command"] = addsource._command(file, GRAPH, o, [])
    if write and res.get("status") == "added":
        data = (root / file).read_bytes()
        mapping = res.get("mapping")
        mrel = Path(mapping).resolve().relative_to(root.resolve()).as_posix() if mapping else None
        man = _manifest(root)
        man.setdefault("steps", []).append({
            "step": "table", "file": file, "sha256": sha256(data), "bytes": len(data),
            "format": (res.get("detected") or {}).get("format"),
            "read": {"template": res["template"], "score": res["score"], "sheet": res.get("sheet"),
                     "bindings": res.get("bindings"), "melted": res.get("melted"), "options": o,
                     "unmapped_columns": res.get("unmapped_columns"), "notes": res.get("notes")},
            "mapping": mrel, "mapping_sha256": sha256((root / mrel).read_bytes()) if mrel else None,
            "dataset": res.get("dataset"), "records": res.get("records"), "results": res.get("results"),
            "verify": (res.get("verify") or {}).get("status"), "command": res["command"]})
        res["manifest"] = _write_manifest(root, man, repo)
        res["mapping"] = mrel
    res.pop("into", None)
    res.pop("written", None)
    return res


def graph(base: str | Path, gid: str, file: str, write: bool = False, repo: Path | None = None,
          step: dict | None = None) -> dict:
    """Merge a graph file under sources/ into the private graph (``scale add-source FILE --merge``): refused on an id
    conflict or another schema version, as the command refuses it."""
    repo = Path(repo) if repo else find_repo_root()
    root = _root(base, gid)
    with _inside(root):
        res = addsource.add(file, GRAPH, repo=repo, kind="graph", merge=True, dry_run=not write)
    cmd = addsource._command(file, GRAPH, {}, ["--merge"])
    res["command"] = cmd
    if write and res.get("status") in ("merged", "unchanged"):
        data = (root / file).read_bytes()
        man = _manifest(root)
        man.setdefault("steps", []).append({"step": "graph", "file": file, "sha256": sha256(data), "bytes": len(data),
                                            "added": res.get("added"), **(step or {}), "command": cmd})
        res["manifest"] = _write_manifest(root, man, repo)
    res.pop("into", None)
    res.pop("written", None)
    return res


def publication(base: str | Path, gid: str, slug: str, doc: dict, rights_yaml: str, about: dict | None = None,
                write: bool = True, repo: Path | None = None) -> dict:
    """A publication curated in the browser (its graph and its rights), kept as sources/<slug>/graph.json and merged
    into the private graph; moved to the private graph's schema version first (``scale graph migrate``) when it pins
    another. ``about``: how it was curated (the PDF's name and sha256, the model), for the manifest; the PDF itself is
    never kept."""
    from .pgraph_migrate import raise_version
    repo = Path(repo) if repo else find_repo_root()
    root = _root(base, gid)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        _fail("a publication's slug is lower-case letters, digits or '-'")
    head = (load_doc(root / GRAPH).get("graph") or {}).get("schema_version")
    have = (doc.get("graph") or {}).get("schema_version")
    migrated = None
    if have != head:
        spec = pgraph.load_spec(pgraph.spec_for(repo, head, None, None))
        doc, _ = raise_version(doc, head, spec)
        migrated = have
    # a folder of its own, named by its content too, so that a later curation of a file of the same name never
    # replaces what an earlier step merged (and the command line still finds it)
    body = json.dumps(doc, sort_keys=True).encode("utf-8") + (rights_yaml or "").encode("utf-8")
    slug = f"{slug[:50]}-{sha256(body)[:10]}"
    folder = root / SOURCES / slug
    fresh = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    write_canonical(folder / GRAPH, doc)
    (folder / "rights.yaml").write_text(rights_yaml or "sources: {}\n", encoding="utf-8")
    step = {"curated": about or {}, **({"migrated_from": migrated} if migrated else {}),
            "rights": f"{SOURCES}/{slug}/rights.yaml"}
    try:
        res = graph(base, gid, f"{SOURCES}/{slug}/{GRAPH}", write=write, repo=repo, step={"publication": step})
    except ScaleError:
        if fresh:
            shutil.rmtree(folder, ignore_errors=True)   # nothing of a refused merge stays
        raise
    if fresh and res.get("status") not in ("merged", "unchanged"):
        shutil.rmtree(folder, ignore_errors=True)
    return res


def sources_of(base: str | Path, gid: str) -> list[dict]:
    """The publications (Source records) the private graph holds, which a table may be linked to (--from-source)."""
    doc = load_doc(_root(base, gid) / GRAPH)
    return [{"id": n["id"], "title": (n.get("props") or {}).get("title") or (n.get("props") or {}).get("name") or n["id"]}
            for n in doc["nodes"] if n.get("label") == "Source"]


def files(base: str | Path, gid: str) -> dict:
    """Every file of the private graph's folder, relative path -> base64 bytes (the working copy, the download)."""
    root = _root(base, gid)
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            out[p.relative_to(root).as_posix()] = base64.b64encode(p.read_bytes()).decode("ascii")
    return out


def restore(base: str | Path, gid: str, contents: dict) -> dict:
    """Write a private graph's folder back from :func:`files` (a working copy kept in the browser)."""
    root = _root(base, gid)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for rel, b64 in contents.items():
        parts = str(rel).split("/")
        if "\\" in rel or ":" in rel or rel.startswith("/") or any(x in ("", ".", "..") for x in parts):
            _fail(f"{rel}: not a path inside the private graph's folder")
        p = (root / rel).resolve()
        if not p.is_relative_to(root.resolve()):
            _fail(f"{rel}: outside the private graph's folder")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(b64))
    if not (root / GRAPH).is_file():
        _fail(f"the working copy of {gid} holds no {GRAPH}")
    return {"id": gid, "files": len(contents)}


def verify(base: str | Path, gid: str) -> dict:
    """``scale data verify graph.json`` in the folder: every cited cell checked against its file under sources/."""
    from .datatable import verify as _verify
    root = _root(base, gid)
    return _verify(load_doc(root / GRAPH), files={}, base=root)


__all__ = ["new", "detect", "drop", "stage", "unstage", "table", "graph", "publication", "sources_of", "files", "restore", "verify", "safe_name"]
