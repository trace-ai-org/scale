// SCALE in the browser: Pyodide runs the scale package (py/scale-py.zip, built by `scale site build`), so a search, a
// query plan or a description here is the code of `scale graph search`, `scale graph query` and `scale describe`.
// Messages: {id, op: "put", name, text} stores a graph; {id, op: "call", tool, args} runs a tool on it. Curating with a
// reader's own key: {id, op: "curate_start", text, name} starts a source folder from its page texts (scale source
// start), {id, op: "curate_call", tool, args} runs one of scale.curation_tools on it, {id, op: "curate_graph"} returns
// the graph.
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
  py.runPython(`
import json
from scale import pgraph
from scale.api import ScaleToolkit
from scale.util import load_doc

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
    started = curate.start(base / "source", text=base / a["name"])
    _cur = CurationTools(base / "source", where="a web page, in the reader's browser")
    return json.dumps({"started": started, "source": _cur.info, "system": _cur.system(), "first": _cur.first(),
                       "tools": _cur.definitions()}, default=str)

def curate_call(tool, args_json):
    return as_json(_cur.call(tool, json.loads(args_json)))

def curate_graph():
    return json.dumps(_cur.graph(), default=str)

def scale_call(tool, args_json):
    a = json.loads(args_json)
    if tool == "stats":                      # what the service's question loop gives as stats
        return json.dumps(pgraph.stats(load_doc("/work/" + a["path"])), default=str)
    return json.dumps(_kit.call(tool, a), default=str)
`);
  call = py.globals.get("scale_call");
  self.curate = { start: py.globals.get("curate_start"), call: py.globals.get("curate_call"), graph: py.globals.get("curate_graph") };
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
    } else if (op === "curate_start") {
      postMessage({ id, ok: true, result: JSON.parse(self.curate.start(JSON.stringify({ text: e.data.text, name: e.data.name }))) });
    } else if (op === "curate_call") {
      postMessage({ id, ok: true, result: self.curate.call(e.data.tool, JSON.stringify(e.data.args || {})) });
    } else if (op === "curate_graph") {
      postMessage({ id, ok: true, result: JSON.parse(self.curate.graph()) });
    } else {
      postMessage({ id, ok: false, error: `unknown op ${op}` });
    }
  } catch (err) {
    postMessage({ id, ok: false, error: String((err && err.message) || err).slice(-2000) });
  }
};
