"""The tools a model curates one source with, by the curate-corpus skill: one set for every place that runs the loop.

The web service (``service/scaleweb/agent.py``) and the graph explorer, which runs this module in the browser with a
reader's own API key (``site/worker.js``), give the model the same tools over a ``scale source start`` folder: the
page texts, the schema, the working graph (read, write, remove) and ``scale source check --derive-links``, until it
calls ``done``. Each tool's definition is vendor-neutral JSON Schema; the caller turns it into its provider's format.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import curate, layout
from .api import ScaleToolkit
from .util import find_repo_root, load_doc, write_canonical

STOP = "done"                  # the tool that ends the loop; its input is the curator's summary


def skill(repo: Path, name: str) -> str:
    """A skill's procedure, without its front matter."""
    text = (Path(repo) / layout.SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties or {}, "required": required or []}}


def short_check(res: dict) -> dict:
    """``scale source check`` cut to what a model acts on: the first 40 errors and 20 warnings."""
    out = {k: res[k] for k in ("ok", "records", "quotes_checked_against_text", "gaps", "cap") if k in res}
    out["errors"] = res.get("errors", [])[:40]
    out["warnings"] = res.get("warnings", [])[:20]
    if len(res.get("errors", [])) > 40:
        out["errors_not_shown"] = len(res["errors"]) - 40
    return out


class CurationTools:
    """The tools over one ``scale source start`` folder, their definitions, the system prompt and the first message."""

    def __init__(self, folder: Path, repo: Path | None = None, where: str = "a web service"):
        self.folder = Path(folder)
        self.repo = Path(repo) if repo else find_repo_root()
        self.where = where
        self.info = load_doc(self.folder / "source.json")
        self.gpath = self.folder / f"{self.info['slug']}.graph.json"
        self.fixed = {n["id"] for n in load_doc(self.gpath)["nodes"]}   # the Source, steward and project it starts with
        self.kit = ScaleToolkit(workspace=self.folder, repo=self.repo)

    # -- the tools
    def pages(self, first: int, last: int) -> str:
        texts = sorted((self.folder / "work" / "texts").glob("*.txt"))
        if not texts:
            return "no page texts: the source had no readable text"
        text = texts[0].read_text(encoding="utf-8")
        chunks = re.split(r"^\[page (\d+)\]\n", text, flags=re.M)[1:]
        pages = {int(chunks[i]): chunks[i + 1] for i in range(0, len(chunks), 2)}
        keep = [f"[page {p}]\n{pages[p]}" for p in sorted(pages) if first <= p <= last]
        return "".join(keep) or f"no pages {first}-{last}; the source has pages {min(pages)}-{max(pages)}"

    def page_count(self) -> int:
        return self.info.get("read", {}).get("pages") or len(re.findall(r"^\[page \d+\]$", self.pages(0, 10**6), re.M))

    def upsert(self, nodes: list[dict], links: list[dict]) -> dict:
        doc = load_doc(self.gpath)
        for key, recs in (("nodes", nodes), ("links", links)):
            have = {r["id"]: i for i, r in enumerate(doc[key])}
            for r in recs or []:
                if not isinstance(r, dict) or not r.get("id"):
                    raise ValueError(f"every {key[:-1]} needs an id")
                if r["id"] in have:
                    doc[key][have[r["id"]]] = r
                else:
                    have[r["id"]] = len(doc[key])
                    doc[key].append(r)
        write_canonical(self.gpath, doc)
        return {"nodes": len(doc["nodes"]), "links": len(doc["links"])}

    def remove(self, ids: list[str]) -> dict:
        doc = load_doc(self.gpath)
        gone = set(ids) - self.fixed
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] not in gone]
        doc["links"] = [e for e in doc["links"]
                        if e["id"] not in gone and e["source"] not in gone and e["target"] not in gone]
        write_canonical(self.gpath, doc)
        return {"removed": sorted(gone), "kept": sorted(set(ids) & self.fixed), "nodes": len(doc["nodes"]),
                "links": len(doc["links"])}

    def summary(self) -> dict:
        doc = load_doc(self.gpath)
        by: dict[str, list[str]] = {}
        for n in doc["nodes"]:
            by.setdefault(n["label"], []).append(n["id"])
        types: dict[str, int] = {}
        for e in doc["links"]:
            types[e["type"]] = types.get(e["type"], 0) + 1
        return {"graph": doc["graph"], "nodes_by_label": by, "links_by_type": types}

    def check(self) -> dict:
        return short_check(curate.check(self.folder, self.repo, derive_links=True))

    def graph(self) -> dict:
        return load_doc(self.gpath)

    # -- what the model is given
    def definitions(self) -> list[dict]:
        prefix = self.info["source_id"].replace("/source/", "/")
        return [
            _tool("read_pages", "The source's page texts, pages first to last (inclusive). Quote evidence verbatim "
                  "from them.", {"first": {"type": "integer"}, "last": {"type": "integer"}}, ["first", "last"]),
            _tool("describe", "The schema: labels and edge types with their properties and the catalysis checklist "
                  "(scale describe). No names: an overview of every label and edge type.",
                  {"names": {"type": "array", "items": {"type": "string"}}}),
            _tool("graph_summary", "The working graph's header, its node ids by label and its link counts by type."),
            _tool("get_records", "Records of the working graph by id, in full.",
                  {"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"]),
            _tool("upsert", "Add records to the working graph, or replace those with the same id. Nodes are "
                  "{id, label, props}; links are {id, type, source, target, props}. Ids start with the source's "
                  f"prefix ({prefix}/…) unless they are shared records.",
                  {"nodes": {"type": "array", "items": {"type": "object"}},
                   "links": {"type": "array", "items": {"type": "object"}}}),
            _tool("remove", "Remove records (and the links touching them) by id. The Source, steward and project stay.",
                  {"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"]),
            _tool("check", "scale source check --derive-links: the graph valid under its schema, every quote found in "
                  "the page texts, conformance, gaps and the copyright cap. Run it after writing and fix every error."),
            _tool(STOP, "Finish: the graph checks without error and holds what the source reports. Say what was "
                  "curated, what was left out and why.", {"summary": {"type": "string"}}, ["summary"]),
        ]

    def call(self, name: str, a: dict):
        """Run one tool; an unknown name or a bad input raises, and the caller hands the error to the model."""
        a = a or {}
        if name == "read_pages":
            return self.pages(int(a["first"]), int(a["last"]))
        if name == "describe":
            return self.kit.call("describe", {"names": a.get("names") or [], "domain": "catalysis"})
        if name == "graph_summary":
            return self.summary()
        if name == "get_records":
            doc, want = load_doc(self.gpath), set(a.get("ids") or [])
            return [r for r in doc["nodes"] + doc["links"] if r["id"] in want]
        if name == "upsert":
            return self.upsert(a.get("nodes") or [], a.get("links") or [])
        if name == "remove":
            return self.remove(a.get("ids") or [])
        if name == "check":
            return self.check()
        raise ValueError(f"unknown tool {name}")

    def system(self) -> str:
        return (skill(self.repo, "curate-corpus") + f"\n\n# Working here\n\nYou curate in {self.where}, through the "
                "tools given (no shell; the scale commands named above are run for you: `check` is `scale source "
                "check --derive-links`). Read every page before you finish. Write records with `upsert`, run `check` "
                "and fix what it reports, then call `done`.")

    def first(self) -> str:
        return (self.folder / "BRIEF.md").read_text(encoding="utf-8") + f"\n\nThe source has {self.page_count()} pages of text."


def as_json(obj) -> str:
    """A tool result as the model reads it: text as it is, anything else as JSON."""
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
