// SCALE in the browser: Pyodide runs the scale package (py/scale-py.zip, built by `scale site build`), so a search, a
// query plan or a description here is the code of `scale graph search`, `scale graph query` and `scale describe`.
// Messages: {id, op: "put", name, text} stores a graph; {id, op: "call", tool, args} runs a tool on it. Curating with a
// reader's own key: {id, op: "curate_start", text, name, version} starts a source folder from its page texts (scale source
// start), {id, op: "curate_call", tool, args} runs one of scale.curation_tools on it, {id, op: "curate_graph"} returns
// the graph; {id, op: "contribution", text} the graph as it is contributed, every quote withheld. {id, op: "table", args:
// {text, spec, path, dataset, sha256, file_sha256}} reads a linked data file the reader attached (scale.plotdata), here.
// {id, op: "lab", fn, args} runs scale.pagelab (a private graph made in the page by Add a source, with the code of
// scale add-source) on the folders under /lab, which live in this worker only; the page keeps a working copy.
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
importScripts(PYODIDE + "pyodide.js");

let call = null;
const ready = (async () => {
  postMessage({ status: "Loading Python (Pyodide)…" });
  const py = await loadPyodide({ indexURL: PYODIDE });
  postMessage({ status: "Loading SCALE…" });
  await py.loadPackage(["pyyaml", "numpy", "sqlite3", "jsonschema"]);
  const zip = await fetch("py/scale-py.zip");
  if (!zip.ok) throw new Error(`py/scale-py.zip: HTTP ${zip.status}`);
  py.unpackArchive(await zip.arrayBuffer(), "zip", { extractDir: "/lib/python3.12/site-packages" });
  py.FS.mkdirTree("/work");
  py.FS.mkdirTree("/lab");
  py.runPython(`
import json
from scale.api import ScaleToolkit

_kit = ScaleToolkit(workspace="/work")

from pathlib import Path
import shutil
from scale import curate
from scale.curation_tools import CurationTools, as_json

_cur = None

def curate_start(args_json):
    global _cur
    a = json.loads(args_json)
    base = Path("/curate")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir()
    (base / a["name"]).write_text(a["text"], encoding="utf-8")
    started = curate.start(base / "source", text=base / a["name"], version=a.get("version") or None,
                           stewardship=a.get("stewardship") or None)   # a private graph's, when it goes into one
    _cur = CurationTools(base / "source", where="a web page, in the reader's browser")
    return json.dumps({"started": started, "source": _cur.info, "system": _cur.system(), "first": _cur.first(),
                       "tools": _cur.definitions()}, default=str)

def curate_call(tool, args_json):
    return as_json(_cur.call(tool, json.loads(args_json)))

def curate_graph():
    return json.dumps(_cur.graph(), default=str)

def contribution(text):                      # the graph as it is contributed: no quoted text of its source kept
    from scale import commons, rights
    doc = json.loads(text)
    sid = commons.sources_of(doc)[0]["id"]
    capped, rep = rights.cap(doc, {sid: {"class": "in_copyright", "licence": "not verified", "quote_chars": 0,
                                         "source_chars": 0, "basis": "contributed from a reader's browser"}})
    return json.dumps({"doc": capped, "withheld": rep["withheld"], "slug": sid.split("/source/", 1)[-1]}, default=str)

def scale_call(tool, args_json):             # every tool, stats included, through the toolkit (scale.api)
    return json.dumps(_kit.call(tool, json.loads(args_json)), default=str)

def plot_table(args_json):                   # a linked data file, read here: its chart data and the rows the graph cites
    from scale import plotdata
    from scale.util import ScaleError
    a = json.loads(args_json)
    try:
        doc = json.loads((Path("/work") / a["path"]).read_text(encoding="utf-8")) if a.get("path") else {"nodes": [], "links": []}
        cited = plotdata.cited_cells(doc, a["dataset"]) if a.get("dataset") else []
        res = plotdata.table_data(a["text"], a["spec"], a.get("sha256"), cited, a.get("file_sha256"))
        return json.dumps({**res, "csv": plotdata.table_csv(res)}, default=str)
    except ScaleError as exc:
        return json.dumps({"error": str(exc)})

from scale import pagelab
from scale.util import ScaleError as _ScaleError
_LAB_FNS = {"new", "detect", "stage", "table", "graph", "publication", "sources_of", "files", "restore", "verify", "drop"}

def lab_call(fn, args_json):                 # a private graph made in this page (scale add-source's code), in /lab
    a = json.loads(args_json)
    if fn not in _LAB_FNS:
        return json.dumps({"error": f"unknown lab function {fn}"})
    if fn == "publication":                  # the paper curated in this page: its graph and its rights
        if _cur is None:
            return json.dumps({"error": "no publication was curated in this page"})
        a.setdefault("doc", _cur.graph())
        rf = Path("/curate/source/rights.yaml")
        a.setdefault("rights_yaml", rf.read_text(encoding="utf-8") if rf.is_file() else "sources: {}\\n")
    try:
        return json.dumps(getattr(pagelab, fn)("/lab", **a), default=str)
    except _ScaleError as exc:
        return json.dumps({"error": str(exc)})
`);
  call = py.globals.get("scale_call");
  self.lab = py.globals.get("lab_call");
  self.plotTable = py.globals.get("plot_table");
  self.curate = { start: py.globals.get("curate_start"), call: py.globals.get("curate_call"), graph: py.globals.get("curate_graph"),
    contribution: py.globals.get("contribution") };
  self.py = py;
  postMessage({ status: "ready" });
})();
ready.catch((err) => postMessage({ status: `SCALE could not start in this browser: ${(err && err.message) || err}` }));

onmessage = async (e) => {
  const { id, op } = e.data;
  try {
    await ready;
    if (op === "put") {
      self.py.FS.writeFile("/work/" + e.data.name, e.data.text);
      postMessage({ id, ok: true });
    } else if (op === "call") {
      postMessage({ id, ok: true, result: JSON.parse(call(e.data.tool, JSON.stringify(e.data.args || {}))) });
    } else if (op === "lab") {
      postMessage({ id, ok: true, result: JSON.parse(self.lab(e.data.fn, JSON.stringify(e.data.args || {}))) });
    } else if (op === "table") {
      postMessage({ id, ok: true, result: JSON.parse(self.plotTable(JSON.stringify(e.data.args || {}))) });
    } else if (op === "curate_start") {
      postMessage({ id, ok: true, result: JSON.parse(self.curate.start(JSON.stringify({ text: e.data.text, name: e.data.name, version: e.data.version || "", stewardship: e.data.stewardship || null }))) });
    } else if (op === "curate_call") {
      postMessage({ id, ok: true, result: self.curate.call(e.data.tool, JSON.stringify(e.data.args || {})) });
    } else if (op === "contribution") {
      postMessage({ id, ok: true, result: JSON.parse(self.curate.contribution(e.data.text)) });
    } else if (op === "curate_graph") {
      postMessage({ id, ok: true, result: JSON.parse(self.curate.graph()) });
    } else {
      postMessage({ id, ok: false, error: `unknown op ${op}` });
    }
  } catch (err) {
    postMessage({ id, ok: false, error: String((err && err.message) || err).slice(-2000) });
  }
};
