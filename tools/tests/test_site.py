"""The static site (scale site build): the repository's graphs and their plans as data, and the scale package the
explorer runs in the browser, which works from its bundled data alone (no checkout), as a wheel does."""

import json
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

from conftest import REPO
from scale import site
from scale.cli import main


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "_site"
    return out, site.build(REPO, out)


def test_the_site_lists_the_curated_graphs(built):
    out, res = built
    entries = json.loads((out / "data/graphs.json").read_text())["graphs"]
    ids = [e["id"] for e in entries]
    assert ids == res["graphs"] and len(set(ids)) == len(ids)
    # the curated graphs, built from their parts; a parts folder with no source yet is left out, examples are not offered
    assert set(ids) == {g.name for g in (REPO / "graphs").iterdir() if any((g / "parts" / "sources").glob("*.json"))}
    assert "methanol-synthesis-pre1966" in ids
    # example questions (graphs/<id>/questions.yaml) go with their graph, for the explorer to offer
    meth = next(e for e in entries if e["id"] == "methanol-synthesis-pre1966")
    assert meth["questions"] and all(q["question"].endswith("?") for q in meth["questions"])
    for e in entries:
        doc = json.loads((out / e["path"]).read_text())
        assert (len(doc["nodes"]), len(doc["links"])) == (e["nodes"], e["links"]), e["id"]
    assert all(e["from"].startswith("graphs/") for e in entries)
    assert (out / "data/query-graph.md").read_text().lstrip().startswith("#")       # the skill, without its front matter
    for page in site.PAGES:
        assert (out / page).is_file(), page


def test_every_example_question_finds_evidence(built):
    """Each example question a graph offers finds quotes of that graph in the page's search (the reader's first look,
    before a model answers it)."""
    from scale.passages import Index, evidence_items, search_quotes
    out, _ = built
    for e in json.loads((out / "data/graphs.json").read_text())["graphs"]:
        if not e.get("questions"):
            continue
        ix = Index.build(evidence_items(json.loads((out / e["path"]).read_text())))
        for q in e["questions"]:
            hits = search_quotes(ix, q["question"], k=5)
            assert len(hits) == 5 and hits[0]["score"] > 0.3, (e["id"], q["question"])


def test_the_package_is_the_wheels_without_the_examples(built):
    out, res = built
    with zipfile.ZipFile(out / site.PACKAGE_ZIP) as z:
        names = set(z.namelist())
        # the code and schemas stay small; the releases it bundles grow with every release, so they are not counted
        code = sum(i.compress_size for i in z.infolist() if not i.filename.startswith("scale/_data/releases/"))
    assert {"scale/api.py", "scale/passages.py", "scale/_data/domain/graph/scale-catal.yaml",
            "scale/_data/core/graph/kernel.yaml", "scale/_data/skills/query-graph/SKILL.md"} <= names
    # what a wheel leaves out (layout.BUNDLE_IGNORE), and the real examples, which the page reads as data instead
    assert not any(n.startswith("scale/_data/examples/") or n.split("/")[-1].startswith("test_") or n.endswith(".pyc")
                   for n in names)
    assert code < 5_000_000 and res["package_bytes"] > code


def test_the_package_runs_without_a_checkout(built, tmp_path):
    """Unpacked anywhere, it reads its schema from scale/_data and answers a plan, as the page does in Pyodide."""
    out, _ = built
    with zipfile.ZipFile(out / site.PACKAGE_ZIP) as z:
        z.extractall(tmp_path / "lib")
    work = tmp_path / "work"
    work.mkdir()
    graph = (out / "data/graphs/methanol-synthesis-pre1966.json").read_text()
    (work / "g.json").write_text(graph)
    sources = sum(n["label"] == "Source" for n in json.loads(graph)["nodes"])
    code = ("import json; from scale.api import ScaleToolkit; from scale import layout; "
            "k = ScaleToolkit(workspace='work'); "
            "r = k.call('query', {'path': 'g.json', 'plan': {'match': {'label': 'Source', 'as': 's'}, 'return': ['s.title']}}); "
            "print(json.dumps([r['status'], r['row_count'], str(layout.__file__)]))")
    proc = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, 'lib'); {code}"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    status, rows, where = json.loads(proc.stdout)
    assert (status, rows) == ("answered", sources) and where.startswith(str(tmp_path / "lib"))


def test_the_cli_builds_the_site(tmp_path, capsys):
    assert main(["--repo", str(REPO), "site", "build", "--out", str(tmp_path / "s")]) == 0
    assert json.loads(capsys.readouterr().out)["graphs"]
    assert (tmp_path / "s/index.html").is_file() and (tmp_path / "s/.nojekyll").is_file()
    assert (tmp_path / "s/bundle.js").is_file()


# A small knowledge-graph bundle, as `scale graph export --to kg-bundle` writes one (docs/kg-bundle-format.md).
BUNDLE_GRAPH = {
    "directed": True, "multigraph": True, "graph": {"bundle_id": "tiny", "topic": "Tiny bundle", "version": "1.0.0"},
    "nodes": [
        {"id": "Publication:10.1/x", "label": "Publication", "props": {"title": "A paper", "year": 1930}},
        {"id": "CatalyticTest:t-1", "label": "CatalyticTest", "props": {
            "name": "test 1", "temperature": {"value": 573.15, "unit": "K", "as_reported": "300 °C"},
            "conditions": json.dumps({"operation": {"status": "present", "text_value": "closed circuit"}}),
            "_value_status": {"pressure": {"status": "not_reported", "missing_reason": "not_stated_in_source"}},
            "_evidence": [{"quote": "at 300 °C", "locator": {"page": 3, "section": "Figure 3", "figure": "Figure 3"},
                           "source_id": "10.1/x"}]}},
    ],
    "links": [{"id": "e:1", "source": "CatalyticTest:t-1", "target": "Publication:10.1/x", "type": "REPORTED_IN", "props": {}}],
}


def test_the_explorer_opens_a_knowledge_graph_bundle_zip(tmp_path):
    page = (REPO / "site/index.html").read_text()
    js = (REPO / "site/explorer.js").read_text()
    # the file button takes a bundle's zip as well as a graph file, and reads it with the page's bundle reader
    assert 'accept=".json,application/json,.zip,application/zip"' in page and "Open a graph or bundle" in page
    assert 'import { readBundle } from "./bundle.js";' in js and "bundle.js" in site.PAGES
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    z = tmp_path / "tiny_v1.0.0.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:      # the folder at the top, as the format ships it
        f.writestr("tiny_v1.0.0/bundle.json", json.dumps({"bundle_id": "tiny", "version": "1.0.0", "topic": "Tiny bundle",
                                                          "graph_file": "kg/graph.json"}))
        f.writestr("tiny_v1.0.0/kg/graph.json", json.dumps(BUNDLE_GRAPH))
        f.writestr("__MACOSX/tiny_v1.0.0/._bundle.json", "")
    script = (f"import {{ readBundle }} from {json.dumps((REPO / 'site/bundle.js').as_uri())};\n"
              f"import {{ readFileSync }} from 'node:fs';\n"
              f"const buf = readFileSync({json.dumps(str(z))});\n"
              "const file = { name: 'tiny_v1.0.0.zip', arrayBuffer: async () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length) };\n"
              "console.log(JSON.stringify(await readBundle(file)));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    b = json.loads(run.stdout)
    assert (b["title"], b["version"]) == ("Tiny bundle", "1.0.0")
    src, test = b["doc"]["nodes"]
    # a Publication is a source, hidden until asked for like any graph's sources; a value reads as reported
    assert src["label"] == "Source" and test["label"] == "CatalyticTest"
    p = test["props"]
    assert p["temperature"] == {"status": "present", "value": 573.15, "unit": "K", "as_reported": "300 °C"}
    assert p["conditions"] == {"operation": {"status": "present", "text_value": "closed circuit"}}
    assert p["pressure"] == {"status": "not_reported", "missing_reason": "not_stated_in_source"}
    assert p["evidence"] == [{"quote": "at 300 °C", "locator": "p. 3, section Figure 3", "source": "Publication:10.1/x"}]
    assert b["doc"]["links"][0]["type"] == "REPORTED_IN"
    # a zip that holds no bundle is refused, saying why
    with zipfile.ZipFile(tmp_path / "other.zip", "w") as f:
        f.writestr("notes.txt", "hello")
    run = subprocess.run([node, "--input-type=module", "-e", script.replace(str(z), str(tmp_path / "other.zip"))],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode != 0 and "no bundle.json" in run.stderr


def parts_bundle(path):
    """Six sources of two samples each, every sample made from the one species they all report; links before nodes,
    and a quote with braces and quotes in it, as the reader must take them."""
    nodes, links = [{"id": "ChemicalSpecies:water", "label": "ChemicalSpecies", "props": {"name": "water"}}], []
    for s in range(6):
        pub = f"Publication:10.1/s{s}"
        nodes.append({"id": pub, "label": "Publication", "props": {"title": f'Paper {s}: "{{a}} [b]"', "year": str(2000 + s)}})
        links.append({"id": f"w:{s}", "source": "ChemicalSpecies:water", "target": pub, "type": "REPORTED_IN", "props": {}})
        for r in range(2):
            rid = f"Sample:s{s}-{r}"
            nodes.append({"id": rid, "label": "Sample", "props": {"name": f"sample {s}.{r}"}})
            links += [{"id": f"r:{rid}", "source": rid, "target": pub, "type": "REPORTED_IN", "props": {}},
                      {"id": f"u:{rid}", "source": rid, "target": "ChemicalSpecies:water", "type": "HAS_INPUT", "props": {}}]
    graph = {"directed": True, "multigraph": True, "graph": {"topic": "Tiny bundle"}, "links": links, "nodes": nodes}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("tiny_v1.0.0/bundle.json", json.dumps({"bundle_id": "tiny", "version": "1.0.0", "topic": "Tiny bundle"}))
        f.writestr("tiny_v1.0.0/kg/graph.json", json.dumps(graph, indent=1))


def test_a_bundle_too_large_to_draw_at_once_is_drawn_a_window_at_a_time_and_searched_whole(tmp_path):
    js, page = (REPO / "site/explorer.js").read_text(), (REPO / "site/index.html").read_text()
    # the explorer draws a large bundle a window at a time (the arrows step through the whole graph, the width is the
    # reader's) and searches it whole; a large graph is drawn on a canvas and laid out off the page's thread
    assert "drawWindow" in js and 'id="window-next"' in js and 'id="window-width"' in js and "searchBundle" in js
    assert 'id="gx-canvas"' in page
    assert 'new Worker("layout.js")' in js and "layout.js" in site.PAGES
    assert "d3/7.9.0/" in (REPO / "site/layout.js").read_text()
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    parts_bundle(tmp_path / "tiny.zip")
    script = (f"import {{ readBundle }} from {json.dumps((REPO / 'site/bundle.js').as_uri())};\n"
              f"import {{ readFileSync }} from 'node:fs';\n"
              f"const buf = readFileSync({json.dumps(str(tmp_path / 'tiny.zip'))});\n"
              "const file = { name: 'tiny.zip', arrayBuffer: async () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length) };\n"
              "const b = await readBundle(file, () => {}, { whole: 10 });\n"
              "console.log(JSON.stringify({ records: b.records, sources: b.sources, years: b.years, start: b.start,\n"
              "  near: b.around(b.start, 7), all: b.around(b.start, 100).records, hits: b.search('sample 3.1', 5),\n"
              "  windows: [0, 7, 13, 999].map((f) => { const w = b.view(null, f, 7); return [w.from, w.to, w.total, w.records, w.years]; }),\n"
              "  y2004: b.startOf(2004),\n"
              "  of: b.sourceOf('Sample:s3-1'), main: b.mainOf(2004) }));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    b = json.loads(run.stdout)
    # 19 records, more than the 10 drawn at once: kept whole, each source with its records and year
    assert b["records"] == 19 and [s["year"] for s in b["sources"]] == list(range(2000, 2006)) and b["years"] == list(range(2000, 2006))
    assert b["sources"][0] == {"title": 'Paper 0: "{a} [b]"', "year": 2000, "records": 3}
    # around a source, up to 7 records: it and the source most like it (the same species, the nearest year); the
    # species every source reports brings in no sample of another source
    near = b["near"]
    assert (near["records"], near["sources"]) == (7, 2)
    assert sorted(n["id"] for n in near["doc"]["nodes"]) == ["ChemicalSpecies:water", "Publication:10.1/s0", "Publication:10.1/s1",
                                                             "Sample:s0-0", "Sample:s0-1", "Sample:s1-0", "Sample:s1-1"]
    assert len(near["doc"]["links"]) == 10              # four REPORTED_IN, four HAS_INPUT, the species in its two sources
    assert near["doc"]["graph"]["window"] == {"order": "around", "from": 0, "to": 7, "total": 19, "sources": 2, "records": 7,
                                              "years": [2000, 2001], "source": "Publication:10.1/s0",
                                              "title": 'Paper 0: "{a} [b]"', "year": 2000}
    assert {n["label"] for n in near["doc"]["nodes"] if n["id"].startswith("Publication:")} == {"Source"}
    assert b["all"] == 19                                # far enough, the whole graph
    # by year, windows of 7 records one after the other cover the whole graph; the species comes with each, as its
    # sources' samples use it; past the end, the last window
    assert b["windows"] == [[0, 7, 19, 7, [2000, 2001]], [7, 13, 19, 7, [2002, 2003]], [13, 19, 19, 7, [2004, 2005]],
                            [16, 19, 19, 4, [2005, 2005]]]
    assert b["y2004"] == 13
    # the search reads every record, drawn or not, and names each match's source
    assert b["hits"] == [{"record": "Sample:s3-1", "label": "Sample", "name": "sample 3.1", "text": "sample 3.1",
                          "source": 3, "sourceTitle": 'Paper 3: "{a} [b]"', "year": 2003}]
    assert (b["of"], b["main"]) == (3, 4)
    # read whole, the same bundle is one graph
    whole = subprocess.run([node, "--input-type=module", "-e", script.replace("{ whole: 10 }", "{}").replace(
        script[script.index("console.log"):], "console.log(JSON.stringify(b.doc.nodes.length));")], capture_output=True, text=True, timeout=60)
    assert whole.returncode == 0, whole.stderr
    assert json.loads(whole.stdout) == 19


def test_the_explorer_runs_scales_own_tools_and_sends_a_readers_key_only_to_their_provider():
    page = (REPO / "site/index.html").read_text()
    js = (REPO / "site/explorer.js").read_text()
    worker = (REPO / "site/worker.js").read_text()
    assert 'src="explorer.js"' in page and 'src="config.js"' in page
    assert "py/scale-py.zip" in worker and "ScaleToolkit" in worker
    # every script comes from a pinned version, and every script from a CDN is checked against its digest
    assert "pyodide/v0.26.4/" in worker and "d3/7.9.0/" in page
    cdn = re.findall(r'<script src="https://cdnjs[^>]*>', page)
    assert cdn and all('integrity="sha384-' in s and 'crossorigin="anonymous"' in s for s in cdn)
    # a signed-in reader's question goes to the service, which holds its own key; the service is never sent a reader's
    assert '"/ask"' in js and "@anthropic-ai/sdk" not in js
    service_call = js[js.index("async function call("):js.index("\n}\n", js.index("async function call("))]
    assert "ownKey" not in service_call and "worker.postMessage({ id, ...msg })" in js
    # a reader's own key is sent once, in the one request to Anthropic's API, by the query-graph skill the site carries
    assert js.count('"x-api-key"') == 1 and 'const ANTHROPIC = "https://api.anthropic.com/v1/messages"' in js
    # or, for OpenAI or OpenRouter, once as a bearer token to the chosen provider's chat completions endpoint, and never
    # carried over to another provider
    assert js.count("Bearer ${ownKey}") == 1 and js.count("fetch(p.url") == 1
    assert 'url: "https://api.openai.com/v1/chat/completions"' in js and 'url: "https://openrouter.ai/api/v1/chat/completions"' in js
    assert "(sameKey ? editing.key : \"\")" in js
    assert '"anthropic-dangerous-direct-browser-access": "true"' in js and 'fetch("data/query-graph.md")' in js
    # the reader's keys, a list with one in use, are kept in the tab unless the reader asks for this device, and
    # forgotten on request; a key saved before the list is moved into it and its old entries cleared
    assert 'store.set("scale-own-keys", ownKeys.length ? JSON.stringify(ownKeys) : "", ownKeep)' in js
    assert 'ownKeys = []; ownActive = ""' in js and 'store.set(k, "", false)' in js
    # a key not scoped to a workspace is sent with the workspace the reader gives
    assert '"anthropic-workspace-id": ownWorkspace' in js
    # the graph's type in its card is a link back to the graph on the canvas, as its schema is a link to the schema
    # "Add a source" (one dialog for any source, which hands a publication to Curate) is a toolbar button like the
    # account's; the Visualization is gone
    assert 'id="add-open"' in page and ">Add a source<" in page and 'id="curate-open"' not in page
    assert "viz-btn" not in page and "plotly" not in page.lower() and "Plotly" not in js
    # the Curate dialog is about a source (a paper, patent or report) and says what happens in four short points
    assert "<h2>Curate a source</h2>" in page and "Curate a publication" not in page and "<summary>More details</summary>" in page
    # the one-source page says "source" too
    one = (REPO / "site/publication.html").read_text()
    assert "<title>SCALE One Source</title>" in one and "publication" not in one.replace("publication.html", "").lower()
    # GitHub sign-in sits in the toolbar for every reader; viewing never needs it
    assert 'id="account-btn"' in page and "function renderAccount()" in js and "/auth/login?return=" in js
    assert "need no account" in js
    # a sign-in is one transaction of the tab that starts it: one place builds the sign-in address, carrying the page's
    # own address (its open graph, #g=…, included) and a nonce; a token is taken only with that tab's nonce
    for page_js in (js, (REPO / "site/app.js").read_text()):
        assert page_js.count("/auth/login?return=") == 1 and "&nonce=${n}" in page_js and "crypto.getRandomValues" in page_js
        # the reader's own graph and a job under way are kept in the tab, so a sign-in or a reload loses neither; the
        # service forgets a job's result only when the page says it has it
        assert '"scale-own"' in page_js and '"scale-job"' in page_js and "/done`" in page_js
        assert "tx.n !==" in page_js and "scale-signin-hash" not in page_js and "scale-signin-from" not in page_js
    assert "location.search + location.hash" in js and all(f'data-signin="{w}"' in js for w in ("toolbar", "curate", "search"))
    assert 'id="graph-show"' in js and "function showGraph()" in js and 'id="schema-toggle"' in js
    # the open graph's caret folds and unfolds its facts; the caret turns with the row's aria-expanded
    css = (REPO / "site/explorer.css").read_text()
    assert "folded = !folded" in js and 'aria-expanded="${open}"' in js and '[aria-expanded="true"] .gx-graph-caret' in css


def test_the_site_publishes_the_leaderboard_with_how_it_scores(built):
    out, res = built
    board = json.loads((out / "data/leaderboard.json").read_text())
    assert board["commons"] == "graphs/catal-commons" and board["with_reach"] is False
    assert set(board) >= {"contributors", "recent", "scoring", "levels"} and res["contributors"] == len(board["contributors"])


def test_a_readers_own_key_runs_unlimited_reads_the_conversation_from_the_cache_and_may_be_asked_to_answer_now():
    js = (REPO / "site" / "explorer.js").read_text(encoding="utf-8")
    loop = js[js.index("async function modelLoop("):js.index("const OWN_OUTPUT")]
    ask = js[js.index("async function askOwnKey("):js.index("const ANSWERED_EARLY")]
    # one loop for every provider and task; each turn moves the cache breakpoints to the newest results
    assert "markCache(messages);\n      const r = await claude(" in loop and "cache_control: { type: \"ephemeral\" }" in loop
    assert ".slice(-2)" in js[js.index("function markCache("):js.index("function markCache(") + 400]
    assert "read from the cache" in js and "prompt_tokens_details?.cached_tokens" in loop
    # no token budget, no turn limit and no tool result cut short: the model sees every row it asked for
    assert not re.search(r"OWN_TOKENS|OWN_TURNS|RESULT_CHARS|clip\(", js)
    assert "content = whole(await run(c.name, c.input));" in loop
    # the reader may ask to finish; the model is told so beside the last results, in both formats
    assert loop.count("if (out.stopped) results.push({ type: \"text\", text: wrapUp });") == 1
    assert "messages.push({ role: \"user\", content: wrapUp })" in loop and loop.count("!calls.length || last") == 1
    assert 'id="answer-now"' in ask and "stopNow: () => answerNow" in ask and "await modelLoop(" in ask


def test_a_readers_own_key_curates_a_pdf_in_the_browser_with_the_services_tools():
    js = (REPO / "site" / "explorer.js").read_text(encoding="utf-8")
    worker = (REPO / "site" / "worker.js").read_text(encoding="utf-8")
    page = (REPO / "site" / "index.html").read_text(encoding="utf-8")
    own = js[js.index("async function curateCore("):js.index("// The commons holds the source already")]
    # with a key set, Curate runs here; the service is used only without one
    assert 'if (curateMode() === "own") return curateOwnKey();' in js and 'ownKey ? "own" : SERVICE ? "service" : ""' in js
    # the PDF is read here by pdf.js, loaded only when needed and checked against its digests
    assert 'pdf.js/3.11.174/' in js and js.count("sha384-") >= 2 and "integrity: PDFJS_SRI[" in js
    # the model fills the graph with scale.curation_tools in Pyodide, by the same loop as questions, until done
    assert 'stop: "done"' in own and 'rpc({ op: "curate_call", tool, args })' in own and "await modelLoop(" in own
    assert "from scale.curation_tools import CurationTools, as_json" in worker and "curate.start(" in worker
    # the dialog names no single provider or model
    assert "Read by Claude" not in page and 'id="curate-terms"' in page
    # a PDF only; the statement of access is about sharing the knowledge derived from it; with a key the dialog
    # names no model or provider and says only what stays private
    assert 'id="curate-doi"' not in page and "text mining" not in page and "share the knowledge it reports" in page
    terms = js[js.index("function curateTerms("):js.index("async function curateAccount(")]
    # a curation says where it is (four stages, a bar) and about how long it has left
    prog = js[js.index("function curateProgress("):js.index("async function curateCore(")]
    assert "min left" in prog and "elapsed" in prog and 'id="cj-bar"' in prog and "st.tool = (tool, args, result)" in prog
    # Curate is grey until a PDF is given and the statement ticked
    sync = js[js.index("function syncCurate("):js.index("const pdfName")]
    assert '!$("curate-pdf").files[0]' in sync and '!$("curate-attest").checked' in sync
    # a graph curated here is contributed through the service, signed in with GitHub for the credit, in one click: a
    # reader signed out is signed in and the contribution goes on by itself when they come back; the pull request opens
    # with no second click
    contrib = js[js.index("async function contribute("):js.index("async function start(")]
    assert "curated_in_browser: true" in js and 'e.from === "own-key"' in js
    assert 'store.set(CONTRIB_PENDING, B.entry.id); signIn("contribute")' in contrib and 'call("POST", "/contribute", contribBody(B))' in contrib
    assert "store.take(CONTRIB_PENDING) === own.entry.id) contribute()" in js
    # a personal graph's card has the rows every graph's has; its Contribute button sits on its title line, before
    # its label (personal, in the site's green); without the service it says it is to come
    rows = js[js.index("function renderGraphs("):js.index("function details(")]
    assert "contribButton()" in rows and 'class="gx-graph-actions"' in rows
    assert '(level === "personal" ? contribButton() : "")' in rows
    # without the service, Contribute downloads the graph with every quote withheld and opens GitHub's upload page for
    # catal-commons/incoming/, where the reader proposes it under their own login; the repository's intake folds it in
    gh = js[js.index("async function contributeOnGitHub("):js.index("async function contribute(")]
    assert "/upload/main/graphs/catal-commons/incoming" in gh and 'op: "contribution"' in gh and "download(file" in gh
    assert '"quote_chars": 0' in worker
    # a source is curated under the schema catal-commons pins, written by the build, so that it folds in after a bump
    assert "version: commonsVersion" in js and 'version=a.get("version") or None' in worker
    # with the GitHub sign-in (service/github-login), one click: a sign-in window, then the page opens the pull request
    # itself, the graph attached (fork, branch, the file under incoming/, the pull request), and that window shows it;
    # the token lives in memory for that one contribution, never in the browser's storage; without the sign-in, the
    # graph downloads and GitHub's upload page opens
    tok = js[js.index("async function contributeWithToken("):js.index("async function contribute(")]
    assert all(x in tok for x in ("/forks", "/merge-upstream", "/git/refs", "/contents/graphs/catal-commons/incoming/", "/pulls"))
    assert 'const GH_API = "https://api.github.com"' in js and js.count("fetch(GH_API") == 1
    assert "if (!SERVICE) return GH_LOGIN ? contributeSignedIn() : contributeOnGitHub();" in js
    assert "scale-github-token" not in js and 'id="gh-dialog"' not in page
    # the reader reviews the pull request on GitHub's page, the graph attached, and creates it; the page then marks it
    assert "/compare/main...${encodeURIComponent(me.login)}:${encodeURIComponent(branch)}?expand=1" in tok
    assert "win.location = compare" in tok and 'gh("POST", `/repos/${REPO}/pulls`' not in tok and "watchForPullRequest(" in tok
    assert "e.origin !== from" in js and "e.data.nonce !== nonce" in js
    assert "curated-download" not in js and "contrib-box" not in js and '["Visibility", `<span title="${esc(tip)}">${esc(label)}</span>' in js
    # once a pull request to catal-commons exists, the label reads catal-commons and links it; the by-hand route
    # changes nothing, since only GitHub knows whether the file was uploaded
    assert ">Catal-Commons</a>" in rows and "sentToCommons()" in rows
    gh = js[js.index("async function contributeOnGitHub("):js.index("// With a GitHub sign-in")]
    assert "contributed(" not in gh and "download(file" in gh
    # a curated graph gets a concise title: the model's (done's title), else its source's, else the PDF's name
    assert "graphTitle(r.stopInput?.title, doc, f.name)" in js
    # when it is done, the dialog shows what was curated in a few numbers and what to do next
    done = js[js.index("function curateDone("):js.index("function curateReset(")]
    assert all(f'id="done-{k}"' in done for k in ("explore", "contrib", "again")) and "gx-done-stats" in done
    # the terms say who reads the text: the reader's provider, with their key, and no one else
    assert "<b>Private.</b>" in terms and "providerName()" in terms and "with your key, and to no one else" in terms


def test_the_built_pages_load_scripts_and_the_package_stamped_with_the_builds_digest(built):
    out, res = built
    stamp = res["stamp"]
    assert re.fullmatch(r"[0-9a-f]{12}", stamp)
    page, js, worker = ((out / n).read_text() for n in ("index.html", "explorer.js", "worker.js"))
    assert f'src="explorer.js?v={stamp}"' in page and f'href="explorer.css?v={stamp}"' in page
    assert f'new Worker("worker.js?v={stamp}")' in js and f'"./bundle.js?v={stamp}"' in js
    assert f'fetch("py/scale-py.zip?v={stamp}")' in worker


def test_the_build_names_the_schema_version_catal_commons_pins(built):
    out, _ = built
    header = json.loads((REPO / "graphs/catal-commons/parts/header.json").read_text())
    assert json.loads((out / "data/graphs.json").read_text())["commons_schema_version"] == header["graph"]["schema_version"]


def test_the_answering_prompt_leaves_the_proposal_procedure_to_a_tool(built):
    """The page's prompt is the query-graph skill without its proposal procedure (fetched by proposal_procedure when a
    question asks what to do) and without the command-line route; the answering rules ask for two routes, typed
    absences, a Found by line and [record id, locator] citations, which the page checks."""
    out, _ = built
    answering = (out / "data/query-graph.md").read_text()
    propose = (out / "data/query-graph-propose.md").read_text()
    assert "call the proposal_procedure tool" in answering and "A proposal's layout" not in answering
    assert "scale gap from-query" not in answering and "## Rules" in answering
    assert "**Propose, when the question asks what to do**" in propose and "A proposal's layout" in propose
    assert len(answering) < 0.6 * (len(answering) + len(propose))
    js = (REPO / "site/explorer.js").read_text()
    for part in ('name: "proposal_procedure"', 'fetch("data/query-graph-propose.md")', "at least two routes",
                 "**Found by:**", "**What is missing:**", "[record id, locator]", "function citeLinks", "Reference checks",
                 "returnedIds(res, seen)", 'view: "model"', "checklist: false", "page.next"):
        assert part in js, part
    assert "verified" not in js[js.index("function checksLine"):js.index("function checksLine") + 1500].replace(
        "never that the answer is verified", "")


def test_citations_become_links_and_the_page_checks_their_references(tmp_path):
    """An answer's [record id, locator] citations become links showing the quote; the reference checks flag an id
    the graph does not hold, a record no tool returned, and a quoted phrase that is not in the cited record's evidence
    (line-break hyphens, page marks, spacing and ellipses aside; a word split across two quotes still found)."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = (REPO / "site/explorer.js").read_text()
    code = js[js.index("let idPrefix"):js.index("\n}\n", js.index("function checksLine")) + 3]
    harness = tmp_path / "t.js"
    harness.write_text("""
global.esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
global.idLinks = (h) => h;
const src = {id: "ex:t/source/s", label: "Source", props: {title: "A study of copper catalysts", year: 1928, kind: "patent"}};
const rec = {id: "ex:t/test/1", label: "CatalyticTest", props: {name: "run 1", evidence: [
  {source: "ex:t/source/s", locator: "p. 3", quote: "sensitive to poison-"},
  {source: "ex:t/source/s", locator: "p. 3", quote: "ing by carbonyl at 400\\u00b0 C. | and later"}]}};
global.G = {doc: {nodes: [src, rec], links: []}, byId: new Map([[src.id, src], [rec.id, rec]])};
""" + code + """
const seen = new Set(["ex:t/test/1"]);
const ok = citeLinks('<p>It was \\u201csensitive to poisoning by carbonyl at 400\\u00b0C\\u201d [ex:t/test/1, p. 3].</p>', seen);
const bad = citeLinks('<p>Also \\u201cnot in the quote at all\\u201d [ex:t/test/1, p. 3]; and [ex:t/nope/2, p. 1]; [ex:t/source/s]; [ex:t/test/1, p. 9].</p>', seen);
const seen2 = new Set(); returnedIds({id_prefix: "ex:t/", rows: [{bound: {t: "~test/1"}}]}, seen2);
const tilde = citeLinks('<p>Run 1 [~test/1, p. 3].</p>', seen2);
console.log(JSON.stringify({ok: ok.checks, bad: bad.checks, html: ok.html, line: checksLine(ok.checks), tilde: tilde.checks, seen2: [...seen2]}));
""")
    out = json.loads(subprocess.run(["node", str(harness)], capture_output=True, text=True, check=True).stdout)
    assert out["ok"]["cited"] == 1 and not out["ok"]["quotesMissing"] and not out["ok"]["unknown"]
    assert 'class="gx-cite"' in out["html"] and "A study of copper catalysts · 1928 · patent · p. 3" in out["html"]
    assert "sensitive to poison-" in out["html"] and "all resolve" in out["line"] and "verified" not in out["line"]
    assert out["bad"]["unknown"] == ["ex:t/nope/2"] and out["bad"]["unreturned"] == ["ex:t/source/s"]
    assert len(out["bad"]["quotesMissing"]) == 1
    assert out["bad"]["locators"] == ["ex:t/test/1: p. 9"] and not out["ok"]["locators"]     # a page the evidence lacks
    # a model view shortens ids with ~ for its id_prefix: returned and cited ids are read in full
    assert out["seen2"] == ["ex:t/test/1"] and out["tilde"]["cited"] == 1 and not out["tilde"]["unknown"]


ANALYZE_PANEL = {"x": {"type": "quantity", "title": "T (°C)", "label": "T"},
                 "y": {"type": "quantity", "title": "X (%)", "label": "X"}, "series": ["a"], "facets": [], "points": [
                     {"id": "g::v", "record": "v", "x": {"type": "value", "value": 1, "unit": "°C"},
                      "y": {"type": "value", "value": 5, "uncertainty": 0.5, "unit": "%"}, "series": "a", "evidence": []},
                     {"id": "g::b", "record": "b", "x": {"type": "value", "value": 2, "unit": "°C"},
                      "y": {"type": "bound", "bound": "upper", "value": 1, "unit": "%"}, "series": "a", "evidence": []},
                     {"id": "g::r", "record": "r", "x": {"type": "range", "lower": 3, "upper": 4, "unit": "°C"},
                      "y": {"type": "value", "value": 2, "unit": "%"}, "series": "a", "evidence": []}]}


def test_the_analyze_view_draws_scale_plot_data_honestly_and_loads_vega_pinned():
    page = (REPO / "site/index.html").read_text()
    js = (REPO / "site/explorer.js").read_text()
    an = (REPO / "site/analyze.js").read_text()
    worker = (REPO / "site/worker.js").read_text()
    # an Analyze button beside the graph view opens the panel; its data is scale_plot_data, run in the worker
    assert 'id="analyze-open"' in page and 'id="analyze"' in page and "analyze.js" in site.PAGES
    assert '"./analyze.js"' in site.STAMPED["explorer.js"] and 'scale("plot_data", { suggest: true })' in js
    assert 'op === "table"' in worker and "plotdata.table_data" in worker
    # vega, vega-lite and vega-embed: pinned versions, checked against their digests, loaded only when a chart is drawn,
    # with a loader that fetches nothing (a chart's data is inline)
    assert all(f"/{v}/" in an for v in ("vega/5.30.0", "vega-lite/5.21.0", "vega-embed/6.26.0")) and an.count('"sha384-') == 3
    assert "crossOrigin" in an and "NO_LOADER" in an and "actions: false" in an
    # a linked file is attached in the inspector, read and digested here (crypto.subtle), sent nowhere
    assert "Attach local file" in js and "sha256Hex(buf)" in js and 'LINKED_FILE_LABELS = ["Dataset", "TimeSeries"]' in js
    # the reader's model gets a plot tool; the chart is drawn in the answer card, the model reads it in pages
    assert '{ name: "plot",' in js and 'view: "model"' in js and "r.charts" in js
    # condition filters read the values present from SCALE; the builder folds after a plot; many panels get a selector;
    # each chart lists its points in a keyboard-reachable table; the last chart of a graph is remembered in the tab
    assert "plotCall({ field:" in js and 'id="an-edit"' in js and "an-pick-" in js and "gx-an-table" in js
    assert "scale-analyze:${G.entry.id}" in js and 'e.key === "Escape"' in js and '"conditions"' in js
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = (f"import * as A from {json.dumps((REPO / 'site/analyze.js').as_uri())};\n"
              f"const p = {json.dumps(ANALYZE_PANEL)};\n"
              "const rows = A.rowsOf(p), spec = A.vegaSpec(p, { mark: 'line' });\n"
              "const count = A.vegaSpec({ ...p, y: { type: 'count', title: 'records' }, x: { type: 'category', title: 'k' }, "
              "buckets: [{ x: 'a', records: 2, sources: 1 }] });\n"
              "const hist = A.vegaSpec({ ...p, y: { type: 'count' }, histogram: { bins: [{ lo: 0, hi: 10, records: 3 }] } });\n"
              "console.log(JSON.stringify({ rows, layers: spec.layer.map((l) => [l.mark.type, l.transform ? "
              "l.transform[0].filter : null]), count: count.layer[0].data.values, hist: hist.layer[0].data.values, "
              "header: A.csvHeader('a,\"b,\"\"c\"\"\"\\n1,2'), via: A.specFrom({ label: 'L', x: 'v:' + "
              "JSON.stringify({ via: [{ edge: 'E', to: 'T' }], path: 'p' }), y: 'count' }), "
              "digest: [A.digestCheck('ABC', 'abc'), A.digestCheck('', 'abc'), A.digestCheck('x', 'abc')], "
              "shown: p.points.map((q) => A.shown(q.y)) }));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    v, b, r = out["rows"]
    assert (v["kind"], v["ylo"], v["yhi"]) == ("value", 4.5, 5.5)                    # an uncertainty is an error bar
    assert (b["kind"], b["marker"]) == ("bound", "triangle-down")                      # at most: an open triangle down
    assert (r["kind"], r["x"], r["x2"], r["xr"]) == ("range", 3, 4, True)              # a range: one end to the other
    assert ["rule", "datum.xr && !datum.yr"] in out["layers"] and ["point", "datum.kind === 'bound'"] in out["layers"]
    assert ["line", "datum.kind === 'value'"] in out["layers"]                         # a line joins values only
    assert out["count"] == [{"x": "a", "records": 2, "sources": 1, "series": ""}] and out["hist"][0]["records"] == 3
    assert out["header"] == ["a", 'b,"c"'] and out["via"]["x"] == {"via": [{"edge": "E", "to": "T"}], "path": "p"}
    assert out["digest"] == ["verified", "not_recorded", "mismatch"] and out["shown"] == ["5 ± 0.5 %", "≤ 1 %", "2 %"]


def test_add_a_source_is_one_dialog_for_any_source_and_keeps_the_work(tmp_path):
    page = (REPO / "site/index.html").read_text()
    js = (REPO / "site/explorer.js").read_text()
    add = (REPO / "site/addsource.js").read_text()
    worker = (REPO / "site/worker.js").read_text()
    # one dialog: a file (drop or pick: PDF, text, CSV, Excel, graph, bundle) or a DOI, and one line on privacy
    assert 'id="add-dialog"' in page and 'id="add-file"' in page and 'id="add-doi"' in page and ".xlsx" in page
    assert "nothing is uploaded" in page and "never contributed" in page
    assert "addsource.js" in site.PAGES and '"./addsource.js"' in site.STAMPED["explorer.js"]
    # what a file is, the Python router says (scale.addsource.detect, through scale.pagelab in the worker)
    assert 'op === "lab"' in worker and "from scale import pagelab" in worker and 'lab("detect"' in add
    assert all(f'"{f}"' in worker for f in ("detect", "stage", "table", "graph", "publication", "files", "restore"))
    # the mapping view: template and score, each role's column (a dropdown) and unit, the wide columns, the questions
    # (a date-time input, the assumptions ticked one by one, the units, the rights), the sheet and header row, what is
    # not read; nothing is added before the dry run says Ready; a binding pins the sheet it was read from
    for x in ('id="map-template"', "data-role=", 'type="datetime-local"', "data-assume=", "data-confirm=", 'id="q-rights"',
              'id="map-sheet"', 'id="map-header"', "Not read (", 'id="map-source"', "Add to the graph", "wide: one result per column"):
        assert x in add, x
    assert 'r.status === "dry_run"' in add and "write: false" in add and "write: true" in add and "o.sheet = r.sheet" in add
    # a new private graph's access is project unless the reader chooses; public is never the default
    assert 'v === "institution" ? " selected"' in add and "prefs.access" not in add and "savePrefs({ project, contributor })" in add
    # do not lose work: a working copy in IndexedDB (every call in try/catch), an Unsaved badge, beforeunload, a
    # Download prompt after every add, one zip; the page says that browser storage can be cleared
    assert "indexedDB.open" in add and add.count("} catch { return") >= 3 and '"beforeunload"' in add
    assert "gx-badge-unsaved" in js and "Download it." in add and "which the browser can clear" in add
    # the page uploads nothing: no request leaves addsource.js; a model sees a table's columns only through the egress
    # gate, after the reader asks and approves
    assert "fetch(" not in add and "XMLHttpRequest" not in add
    # a publication needs the reader's own key, names the provider its text goes to, and is curated into the private
    # graph chosen; a download reopens as a private graph; a changed reading asks again what was confirmed
    assert "env.key()" in add and "with your key, and to no" in add and "curateCore(file, el, bound)" in js
    assert "privateGraphIn(await readZip(" in add and "function reconfirm()" in add
    assert "egressGate(levels, provider" in js and "async function modelHelp(" in js and "egressApprove(levels, provider" in js
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    zpath = tmp_path / "t.zip"
    files = "[{ name: 'g/graph.json', data: enc.encode('{\"a\": 1}') }, { name: 'g/sources/é.csv', data: new Uint8Array([0, 255, 10]) }]"
    script = (f"import * as A from {json.dumps((REPO / 'site/addsource.js').as_uri())};\n"
              "import { writeFileSync } from 'node:fs';\n"
              "const enc = new TextEncoder();\n"
              f"const z = A.zipStore({files}), again = A.zipStore({files});\n"
              f"writeFileSync({json.dumps(str(zpath))}, z);\n"
              "const back = await A.readZip(z), m = (o) => enc.encode(JSON.stringify(o));\n"
              "const pg = A.privateGraphIn(new Map([['x/manifest.json', m({ manifest_version: 1, reproduce: {} })], ['x/graph.json', m({})], ['x/README.txt', m('r')]]));\n"
              "const bad = z.slice(); bad[bad.indexOf(0x61, 42)] ^= 1;\n"
              "const damaged = await A.readZip(bad).then(() => 'read', (e) => e.message);\n"
              "const notPg = A.privateGraphIn(new Map([['b/bundle.json', m({})], ['b/kg/graph.json', m({})]]));\n"
              "console.log(JSON.stringify({ damaged, back: [...back].map(([n, b]) => [n, [...b]]), pg: Object.keys(pg.files), notPg, noZip: await A.readZip(enc.encode('plain')),\n"
              "  same: Buffer.from(z).equals(Buffer.from(again)), crc: A.crc32(enc.encode('123456789')),\n"
              "  b64: A.b64ToText(A.bytesToB64(enc.encode('héllo'))), iso: A.isoUTC('2026-03-02T10:00'), bad: A.isoUTC('2026-03-02'),\n"
              "  local: A.localOf('2026-03-02T10:00:00Z'), id: A.labId('CO2RR Lab data!', () => 0.5),\n"
              "  opts: A.cleanOpts({ a: '', b: [], c: {}, d: ['x', 'x'], e: 0, f: { r: 'C' } }),\n"
              "  note: [A.pageNote(\"sheet 'FE' of 'A', 'FE'; give --sheet to choose another\"),\n"
              "         A.pageNote('the header row was found, not given: row 4; give --header-row to settle it')],\n"
              "  gaps: A.checklistGaps({ checklist: { a: { missing: 2 }, b: { missing: 3 } } }),\n"
              "  cells: A.cellsChecked({ verify: { datasets: [{ cells: 4 }, { cells: 5 }] } }),\n"
              "  readme: A.readme({ id: 'g', tool: 'scale-catal 0', schema: { version: '0.8.4' }, graph: { title: 'T', sha256: 'abc',\n"
              "    nodes: 1, links: 0, access: 'project' }, reproduce: { commands: ['scale data new graph.json'] } }) }));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60,
                         env={"TZ": "UTC", "PATH": ""})
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    with zipfile.ZipFile(zpath) as z:                 # a zip Python reads, its CRCs right, the same bytes every time
        assert z.testzip() is None and z.namelist() == ["g/graph.json", "g/sources/é.csv"]
        assert z.read("g/graph.json") == b'{"a": 1}' and z.read("g/sources/é.csv") == b"\x00\xff\n"
    assert out["same"] and out["crc"] == 0xCBF43926 and out["b64"] == "héllo"
    # the page reads its own download back: a private graph's folder (its README left out), never a bundle or another file
    assert out["back"] == [["g/graph.json", list(b'{"a": 1}')], ["g/sources/é.csv", [0, 255, 10]]]
    assert "damaged" in out["damaged"]                  # a byte changed after the download: refused by its checksum
    assert out["pg"] == ["manifest.json", "graph.json"] and out["notPg"] is None and out["noZip"] is None
    assert (out["iso"], out["bad"], out["local"]) == ("2026-03-02T10:00:00Z", "", "2026-03-02T10:00")
    assert re.fullmatch(r"co2rr-lab-data-[0-9a-z]{4}", out["id"])
    assert out["opts"] == {"d": ["x"], "e": 0, "f": {"r": "C"}}
    assert out["note"] == ["sheet 'FE' of 'A', 'FE': choose another sheet above",
                           "the header row was found, not given: row 4: set the header row above if it is another"]
    assert (out["gaps"], out["cells"]) == (5, 9)
    assert "scale data new graph.json" in out["readme"] and "browser can clear" in out["readme"] and "sha256 abc" in out["readme"]


def test_a_batch_takes_up_to_ten_sources_of_any_kind_into_one_private_graph():
    page = (REPO / "site/index.html").read_text()
    js = (REPO / "site/explorer.js").read_text()
    add = (REPO / "site/addsource.js").read_text()
    # several files at once (the picker and a drop), at most MAX_BATCH, the others named as left out
    assert '<input type="file" id="add-file" multiple' in page and "export const MAX_BATCH = 10;" in add
    assert "files.slice(room)" in add and "left out" in add and 'data-remove="' in add
    # each file's kind (paper, table, graph, bundle, a download, a DOI) and its state, one row each
    for x in ('paper: "paper"', 'table: "table"', 'graph: "graph"', 'bundle: "bundle"', 'confirm: "needs your confirmation"',
              '"needs key": "needs your key"', 'skipped: "skipped"', 'failed: "failed"'):
        assert x in add, x
    # one private graph made once; graphs merge first, then each table confirmed (or skipped), then papers in turn
    run = add[add.index("  async function run() {"):add.index("  async function mergeItem(")]
    assert run.count("await target(c)") == 1
    assert run.index("mergeItem(l, it)") < run.index("confirmTable(l, it") < run.index("curateItem(l, it")
    assert "Skip this table" in add and 'finishTable("skipped", "skipped by you")' in add and "Paper ${n} of ${of}" in add
    # no key: papers wait for one, the rest goes in; with a key, one lawful-access statement for the papers
    assert 'settle(it, "needs key"' in add and 'id="batch-attest"' in add and "Tick the statement about the papers first" in add
    # a failure is that file's; a reload mid-batch names the files not added; the tab asks before it closes mid-batch
    assert 'settle(it, "failed"' in add and 'BATCH_NOTE = "scale-add-batch"' in add and "!unsaved().length && !B.running" in add
    assert "stopped before it ended" in js
    # the Curate dialog hands several files, or a data or graph file, to Add a source; the board's button opens it
    assert "curateOrAdd(fs)" in js and "addUI.take(fs)" in js and '$("board-curate").onclick = () => { $("board-dialog").close(); addUI.open(); };' in js
    assert '<input type="file" id="curate-pdf" multiple' in page


def test_analyze_is_the_toolbar_twin_of_add_a_source():
    page = (REPO / "site/index.html").read_text()
    js = (REPO / "site/explorer.js").read_text()
    css = (REPO / "site/explorer.css").read_text()
    bar = page[page.index('<header class="gx-toolbar">'):page.index('<form class="gx-search"')]
    # right after Add a source, the same class, its own chart icon, the panel it controls; the canvas button is gone
    assert bar.index('id="add-open"') < bar.index('id="analyze-open"') and bar.count('class="gx-curate-open"') == 2
    assert 'aria-controls="analyze"' in bar and 'aria-pressed="false"' in bar and "<span>Analyze</span>" in bar
    assert 'id="analyze-btn"' not in page and "analyze-btn" not in js
    # never a dead click: it says why when there is no graph, the graph is loading or holds no numbers
    assert "function analyzeWhyNot()" in js and 'b.setAttribute("aria-disabled", String(!!why))' in js
    assert "if (on && analyzeWhyNot()) { status(analyzeWhyNot()); return; }" in js
    # closing returns focus to it; at phone width both buttons are icons
    assert '$("analyze-close").onclick = () => { openAnalyze(false); $("analyze-open").focus(); };' in js
    assert ".gx-curate-open span" in css and '.gx-curate-open[aria-disabled="true"]' in css


def test_each_graph_has_one_level_from_one_table_decided_by_what_its_document_says(tmp_path):
    js = (REPO / "site/explorer.js").read_text()
    css = (REPO / "site/explorer.css").read_text()
    table = js[js.index("const LEVELS = {"):js.index("const levelCache")]
    # five levels, each with its label, colour and one-line definition; "curated" is no level
    labels = re.findall(r'label: "([^"]+)"', table)
    assert labels == ["Catal-Commons", "Example", "Personal", "Team", "Private"] and table.count("tip: ") == 5
    assert len(set(re.findall(r'colour: "([^"]+)"', table))) == 5 and '"curated"' not in js and "SCOPE" not in js
    # the shared graph and the shipped examples by where they come from; any other by its document, as the egress gate
    # reads it (closedLevels): none closed Personal, institution Private, else Team; unknown is the strictest
    lk = js[js.index("function levelKey("):js.index("const synthetic")]
    assert 'return "commons"' in lk and 'return "example"' in lk and "closedLevels(doc)" in lk
    assert '!closed.length ? "personal" : closed.includes("institution") ? "private" : "team"' in lk and 'if (!doc) return "private"' in lk
    # one casing: as the table writes it, never upper-cased; every badge carries its definition; Unsaved stays apart
    assert "text-transform: uppercase" not in css[css.index(".gx-graph-scope {"):css.index(".gx-graph-detail")]
    assert 'title="${esc(tip)}' in js and "${levelBadge(e, !L.saved)}" in js and "levelBadge(e)" in js
    # Unsaved is a dot on the level's badge (its tooltip and its label kept), so a title keeps its room; graphs of one
    # title are told apart by their order
    assert 'class="gx-badge-unsaved" role="img" aria-label="Unsaved, not downloaded"' in js and ".gx-graph-scope.is-unsaved" in css
    assert "`(${nth.get(e)}) ${e.title}`" in js
    # Contribute only on a Personal graph; a synthetic example says so in its tooltip; the picker names the level
    assert '(level === "personal" ? contribButton() : "")' in js and "synthetic: made up to show the format" in js
    add = (REPO / "site/addsource.js").read_text()
    assert "env.levelLabel(l.entry)" in add and '["institution", "Private: your lab only (access institution)"]' in add
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    # the rule, run on documents of each access: the explorer's own functions, with the egress module's closedLevels,
    # each document given as the one a reader opened from a file
    src = js[js.index("const LEVELS = {"):js.index("const levelBadge")]
    mod = tmp_path / "levels.mjs"
    mod.write_text(f"import {{ closedLevels }} from {json.dumps((REPO / 'site/egress.js').as_uri())};\n"
                   "let own = null; const G = null, labOf = () => null; export function setOwn(o) { own = o; }\n"
                   + src + "\nexport { levelOf };\n")
    st = lambda **k: {"graph": {"stewardship": k} if k else {}, "nodes": [], "links": []}   # noqa: E731
    docs = {"public": st(access="public"), "project": st(access="project"), "consortium": st(access="consortium"),
            "institution": st(access="institution"), "none": st(),
            "embargoed": st(access="embargoed", embargo_until="2999-01-01", access_after_embargo="public"),
            "mixed": {**st(access="public"), "nodes": [{"id": "x", "label": "Sample", "props": {"stewardship": {"access": "project"}}}]}}
    script = (f"import * as M from {json.dumps(mod.as_uri())};\n"
              f"const docs = {json.dumps(docs)}, out = {{}};\n"
              "for (const [k, d] of Object.entries(docs)) { const e = { id: k, from: '' }; M.setOwn({ entry: e, doc: d }); out[k] = M.levelOf(e)[3]; }\n"
              "out.commons = M.levelOf({ id: 'catal-commons', from: 'graphs/catal-commons' })[3];\n"
              "out.example = M.levelOf({ id: 'm', from: 'graphs/methanol' })[3];\n"
              "out.synth = M.levelOf({ id: 's', from: 'domain/x', data_status: 'synthetic_example' })[2];\n"
              "console.log(JSON.stringify(out));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert {k: out[k] for k in docs} == {"public": "personal", "project": "team", "consortium": "team", "institution": "private",
                                         "embargoed": "private", "none": "private", "mixed": "team"}
    assert out["commons"] == "commons" and out["example"] == "example" and "synthetic" in out["synth"]
