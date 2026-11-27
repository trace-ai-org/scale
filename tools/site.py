"""The static site (GitHub Pages): browse, search and query the repository's graphs in a browser, with no server.

``scale site build --out DIR`` writes the site into DIR:

* the pages of ``site/``: ``index.html`` (the graph explorer) and ``publication.html`` (one source into a graph,
  which needs the web service, service/README.md);
* ``data/graphs.json``, the graphs the explorer offers, and ``data/graphs/<id>.json``: the curated graphs
  (``graphs/*``), each built from its parts as ``scale graph build`` does (the examples are not offered);
* ``data/query-graph.md``, the query-graph skill, which a question asked in the page follows;
* ``data/leaderboard.json``, the contributors of catal-commons scored and ranked from its ``contributors.yaml``
  (``commons.boards``: all time and the last 90 days, with how scoring works; the reach of each source is the web
  service's to count, so the page asks the service for it when one is set up);
* ``py/scale-py.zip``, the ``scale`` package with the data a wheel bundles (``layout.BUNDLE``, without the repository's
  examples), which the page runs in Pyodide: a search, a plan or a description in the page is the same code as
  ``scale graph search``, ``scale graph query`` and ``scale graph describe``, and gives the same rows.

Nothing in DIR is committed; the pages workflow (.github/workflows/pages.yml) builds and publishes it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

from . import commons, graphparts, layout
from .util import ScaleError, write_canonical

SITE = "site"
PAGES = ("index.html", "explorer.css", "explorer.js", "analyze.js", "addsource.js", "bundle.js", "egress.js", "layout.js", "worker.js", "publication.html", "app.js", "config.js", "style.css")
PACKAGE_ZIP = "py/scale-py.zip"
#: The bundled items the page never reads (the real examples' parts, texts and PDFs are tens of MB; their graphs are
#: offered as data instead).
NOT_IN_PACKAGE = (layout.EXAMPLES,)


def _entry(doc: dict, gid: str, origin: str) -> dict:
    g = doc.get("graph") or {}
    return {"id": gid, "title": g.get("title") or gid, "description": g.get("description") or "",
            "schema_version": g.get("schema_version"), "data_status": g.get("data_status") or "curated",
            "nodes": len(doc.get("nodes") or []), "links": len(doc.get("links") or []), "from": origin,
            "path": f"data/graphs/{gid}.json"}


def questions(folder: Path) -> list[dict]:
    """A graph folder's example questions (``questions.yaml``: ``questions``, each a ``question`` and a ``kind``), the
    explorer's suggestions for what to ask; none when there is no such file."""
    from .util import load_yaml
    f = Path(folder) / "questions.yaml"
    if not f.is_file():
        return []
    qs = (load_yaml(f) or {}).get("questions") or []
    bad = [q for q in qs if not (isinstance(q, dict) and isinstance(q.get("question"), str) and q["question"].strip())]
    if bad:
        raise ScaleError(f"{f}: every entry of questions is a mapping with a question (text) and a kind")
    return [{"question": q["question"].strip(), **({"kind": q["kind"]} if q.get("kind") else {})} for q in qs]


def graphs(repo: Path) -> list[tuple[str, str, dict]]:
    """(id, origin, document) for every graph the site offers: the curated graphs (``graphs/*``), each built from its
    parts. A folder of parts with no source yet is left out; the examples (``examples/``, the domain's synthetic ones)
    are not offered."""
    out = []
    for folder in sorted(p for p in (repo / layout.GRAPHS).glob("*") if (p / "parts" / "header.json").is_file()):
        if any((folder / "parts" / "sources").glob("*.json")):
            out.append((folder.name, f"{layout.GRAPHS}/{folder.name}", graphparts.build_folder(folder)))
    return out


def package_zip(repo: Path, target: Path) -> int:
    """The ``scale`` package as a wheel lays it out (``scale/*.py`` and ``scale/_data/``), without tests and without
    NOT_IN_PACKAGE; returns the number of files."""
    ignore = layout.bundle_ignore(repo)
    n = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((repo / "tools").glob("*.py")):
            if not any(fnmatch.fnmatch(f.name, x) for x in layout.BUNDLE_IGNORE):
                z.write(f, f"scale/{f.name}")
                n += 1
        for item in layout.BUNDLE:
            if item in NOT_IN_PACKAGE:
                continue
            src = repo / item
            if src.is_file():
                z.write(src, f"scale/_data/{item}")
                n += 1
                continue
            for d, dirs, files in _walk(src, ignore):
                for name in files:
                    p = d / name
                    z.write(p, f"scale/_data/{p.relative_to(repo).as_posix()}")
                    n += 1
    return n


def _walk(top: Path, ignore):
    """os.walk with a copytree ignore callable applied to every folder."""
    stack = [top]
    while stack:
        d = stack.pop()
        names = sorted(p.name for p in d.iterdir())
        skip = ignore(str(d), names)
        dirs = [n for n in names if n not in skip and (d / n).is_dir()]
        files = [n for n in names if n not in skip and (d / n).is_file()]
        yield d, dirs, files
        stack.extend(d / n for n in reversed(dirs))


def _skill_body(repo: Path, name: str) -> str:
    text = (repo / layout.SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


# The query-graph skill's parts that only a proposal needs (how to make a material, how to run a test): step 6 and the
# proposal's layout in step 7. The page's answering prompt leaves them out and fetches them when a question asks for a
# proposal (data/query-graph-propose.md, the explorer's proposal_procedure tool); the command-line route (step 8) is
# not the page's. Each part runs from its marker to the next.
PROPOSE_FROM, PROPOSE_TO = "6. **Propose", "7. **Present"
LAYOUT_FROM, LAYOUT_TO = "   **A proposal's layout", "8. **Route failures"
ROUTE_FROM, ROUTE_TO = "8. **Route failures", "## Rules"


def _cut(text: str, start: str, end: str) -> tuple[str, str]:
    """``text`` without the part from the line starting with ``start`` to the line starting with ``end``, and that part."""
    a, b = text.find("\n" + start), text.find("\n" + end)
    if a < 0 or b < a:
        raise ScaleError(f"skills/query-graph/SKILL.md: no part from {start!r} to {end!r} (the site splits the skill there)")
    return text[:a + 1] + text[b + 1:], text[a + 1:b + 1]


def answering_parts(skill: str) -> tuple[str, str]:
    """(the answering prompt, the proposal procedure) cut from the query-graph skill's body."""
    text, propose = _cut(skill, PROPOSE_FROM, PROPOSE_TO)
    text, layout_ = _cut(text, LAYOUT_FROM, LAYOUT_TO)
    text, _ = _cut(text, ROUTE_FROM, ROUTE_TO)
    pointer = ("6. **Propose, when the question asks what to do** (how to make a material, how to run a test): first call "
               "the proposal_procedure tool and follow the procedure it returns.\n")
    text = text.replace("\n" + PROPOSE_TO, "\n" + pointer + PROPOSE_TO, 1)
    return text, "# Proposing from the graph\n\n" + propose + "\n" + layout_


# Each reference a page makes to a script, a stylesheet or the package, stamped with the build's digest (?v=…), so a
# browser never runs a page of one build with a worker or package of another it kept in its cache.
STAMPED = {
    "index.html": ['href="explorer.css"', 'src="explorer.js"'],
    "explorer.js": ['"./bundle.js"', '"./egress.js"', '"./analyze.js"', '"./addsource.js"', 'new Worker("worker.js")', 'new Worker("layout.js")', 'fetch("data/query-graph.md")'],
    "worker.js": ['fetch("py/scale-py.zip")'],
    "publication.html": ['href="style.css"', 'src="app.js"'],
}


def _stamp(out: Path) -> str:
    """Stamp the built pages' references (``STAMPED``) with the digest of what they load; returns the stamp."""
    h = hashlib.sha256()
    for name in sorted(PAGES) + [PACKAGE_ZIP, "data/query-graph.md", "data/query-graph-propose.md"]:
        h.update(name.encode() + b"\0" + (out / name).read_bytes())
    stamp = h.hexdigest()[:12]
    for name, refs in STAMPED.items():
        text = (out / name).read_text(encoding="utf-8")
        for ref in refs:
            if text.count(ref) != 1:
                raise ScaleError(f"site/{name}: expected one {ref}, found {text.count(ref)}")
            q = ref.rindex('"')
            text = text.replace(ref, f"{ref[:q]}?v={stamp}{ref[q:]}")
        (out / name).write_text(text, encoding="utf-8")
    return stamp


def build(repo: Path, out: Path) -> dict:
    """Write the site into ``out`` (emptied first); returns what was written."""
    repo, out = Path(repo), Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "data" / "graphs").mkdir(parents=True)
    for name in PAGES:
        shutil.copy2(repo / SITE / name, out / name)
    entries = []
    for gid, origin, doc in graphs(repo):
        write_canonical(out / "data" / "graphs" / f"{gid}.json", doc)
        entries.append({**_entry(doc, gid, origin), **({"questions": qs} if (qs := questions(repo / origin)) else {})})
    # the schema version catal-commons pins: a source curated in the page is curated under it, so that it folds in
    # (catal-commons stays behind a schema bump until a person migrates it)
    header = repo / layout.GRAPHS / "catal-commons" / "parts" / "header.json"
    commons_version = json.loads(header.read_text(encoding="utf-8"))["graph"]["schema_version"] if header.is_file() else None
    write_canonical(out / "data" / "graphs.json", {"graphs": entries, "commons_schema_version": commons_version})
    answering, propose = answering_parts(_skill_body(repo, "query-graph"))
    (out / "data" / "query-graph.md").write_text(answering, encoding="utf-8")            # the page's answering prompt
    (out / "data" / "query-graph-propose.md").write_text(propose, encoding="utf-8")      # fetched for a proposal
    boards = commons.boards(repo / layout.GRAPHS / "catal-commons")
    board = boards["contributors"]
    write_canonical(out / "data" / "leaderboard.json", {"commons": f"{layout.GRAPHS}/catal-commons", **boards})
    files = package_zip(repo, out / PACKAGE_ZIP)
    stamp = _stamp(out)
    (out / ".nojekyll").write_text("")
    return {"out": str(out), "graphs": [e["id"] for e in entries], "contributors": len(board), "package_files": files,
            "package_bytes": (out / PACKAGE_ZIP).stat().st_size, "stamp": stamp}


