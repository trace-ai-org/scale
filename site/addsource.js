// Add a source (the graph explorer, docs/data.md): any source into a private graph, in this browser, by the code of
// `scale add-source` (scale.pagelab in the worker). A file is read by the Python router (scale.addsource.detect), so
// the page and the command line agree on what it is: a table (CSV, .xlsx) is matched to the built-in mappings and shown
// for the reader to confirm (the template and its score, each role's column and unit, the wide columns, the questions,
// what will not be read) before `scale add-source` adds it; a publication (PDF, text) goes to the in-browser curation; a
// graph file opens, or merges into a private graph. A private graph lives in this tab (the worker) and in a working
// copy in this browser's storage (IndexedDB), which a browser may clear: its download (one zip: the graph, the mappings,
// the sources and a manifest that rebuilds it on the command line) is the durable copy, and the page says so until it
// is downloaded. Nothing is uploaded; a private graph is never contributed.

// ------------------------------------------------------------------------------------------ pure helpers
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
export function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
// A zip of the entries as they are (method store), names in UTF-8, one fixed date (1980-01-01), so the same files give
// the same bytes: [{name, data: Uint8Array}] -> Uint8Array.
export function zipStore(entries) {
  const enc = new TextEncoder(), parts = [], central = [];
  let offset = 0;
  const u16 = (v) => [v & 0xff, (v >>> 8) & 0xff];
  const u32 = (v) => [v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff];
  const DATE = (0 << 9) | (1 << 5) | 1, TIME = 0;            // 1980-01-01 00:00
  for (const e of entries) {
    const name = enc.encode(e.name), data = e.data, crc = crc32(data);
    const common = [...u16(20), ...u16(0x0800), ...u16(0), ...u16(TIME), ...u16(DATE), ...u32(crc), ...u32(data.length), ...u32(data.length), ...u16(name.length), ...u16(0)];
    const local = new Uint8Array([...u32(0x04034b50), ...common]);
    parts.push(local, name, data);
    central.push(new Uint8Array([...u32(0x02014b50), ...u16(20), ...common, ...u16(0), ...u16(0), ...u16(0), ...u32(0), ...u32(offset)]), name);
    offset += local.length + name.length + data.length;
  }
  const cdSize = central.reduce((n, p) => n + p.length, 0);
  const end = new Uint8Array([...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(entries.length), ...u16(entries.length), ...u32(cdSize), ...u32(offset), ...u16(0)]);
  const all = [...parts, ...central, end], out = new Uint8Array(all.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of all) { out.set(p, at); at += p.length; }
  return out;
}
export function bytesToB64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
export function b64ToBytes(b64) {
  const s = atob(b64), out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}
export const b64ToText = (b64) => new TextDecoder().decode(b64ToBytes(b64));
// The files of a zip (stored, or deflated when it was zipped again), name -> bytes; null when it is no zip.
export async function readZip(bytes) {
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) if (v.getUint32(i, true) === 0x06054b50) { end = i; break; }
  if (end < 0) return null;
  const count = v.getUint16(end + 10, true), cd = v.getUint32(end + 16, true), dec = new TextDecoder();
  const out = new Map();
  for (let k = 0, p = cd; k < count; k++) {
    if (v.getUint32(p, true) !== 0x02014b50) throw new Error("a damaged zip file");
    const method = v.getUint16(p + 10, true), crc = v.getUint32(p + 16, true), csize = v.getUint32(p + 20, true);
    const nlen = v.getUint16(p + 28, true), xlen = v.getUint16(p + 30, true), clen = v.getUint16(p + 32, true), local = v.getUint32(p + 42, true);
    const name = dec.decode(bytes.subarray(p + 46, p + 46 + nlen));
    p += 46 + nlen + xlen + clen;
    if (name.endsWith("/") || name.startsWith("__MACOSX/")) continue;
    const start = local + 30 + v.getUint16(local + 26, true) + v.getUint16(local + 28, true);
    const raw = bytes.subarray(start, start + csize);
    let data;
    if (method === 0) data = raw.slice();
    else if (method === 8) data = new Uint8Array(await new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer());
    else throw new Error(`${name}: compressed in a way this page cannot read (method ${method})`);
    if (crc32(data) !== crc) throw new Error(`${name}: damaged (its checksum does not match)`);
    out.set(name, data);
  }
  return out;
}
// A private graph's download among a zip's files: its folder's files (path -> bytes) when it holds a manifest of this
// page and its graph, else null (a knowledge-graph bundle, or another zip).
export function privateGraphIn(files) {
  if (!files) return null;
  const m = [...files.keys()].filter((n) => /(^|\/)manifest\.json$/.test(n)).sort((a, b) => a.length - b.length)[0];
  if (!m) return null;
  const dir = m.slice(0, -"manifest.json".length);
  let man;
  try { man = JSON.parse(new TextDecoder().decode(files.get(m))); } catch { return null; }
  if (!man || !man.manifest_version || !man.reproduce || !files.has(`${dir}graph.json`)) return null;
  const out = {};
  for (const [n, b] of files) if (n.startsWith(dir) && n !== `${dir}README.txt`) out[n.slice(dir.length)] = b;
  return { manifest: man, files: out };
}
// A date-time input's value (local time, "2026-03-02T10:00") as SCALE's recorded_at takes it: ISO 8601 in UTC with Z.
export function isoUTC(local) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(String(local || ""))) return "";
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().replace(/\.\d{3}Z$/, "Z");
}
// ...and back, for the input (local time).
export function localOf(iso) {
  const d = new Date(iso || "");
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
// A private graph's id from its title: lower-case letters, digits and '-', with a short random tail.
export function labId(title, rand = Math.random) {
  const stem = String(title || "graph").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "graph";
  return `${stem}-${Math.floor(rand() * 36 ** 4).toString(36).padStart(4, "0")}`;
}
// The options `scale add-source` takes from the reader's answers (state.opts), without empty ones; confirm de-duplicated.
export function cleanOpts(o) {
  const out = {};
  for (const [k, v] of Object.entries(o || {})) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) { if (v.length) out[k] = [...new Set(v)]; continue; }
    if (typeof v === "object") { if (Object.keys(v).length) out[k] = { ...v }; continue; }
    out[k] = v;
  }
  return out;
}
// What the download's README says: what the zip holds and how the command line rebuilds the graph.
export function readme(man) {
  const steps = (man.reproduce?.commands || []).map((c) => `    ${c}`).join("\n");
  return `${man.graph?.title || man.id}: a private SCALE graph, made in the graph explorer in a browser.

This download is the durable copy of the graph: the page keeps only a working copy in the browser's storage, which a
browser can clear.

  graph.json        the graph (scale-catal ${man.schema?.version}, ${man.graph?.nodes} records, ${man.graph?.links} links)
  manifest.json     what made it: the tool and schema versions (the spec's sha256), each source's sha256 and how it
                    was read, and the commands below
  rights.yaml       the rights of the sources it quotes
  sources/          the files added (a publication's curated graph, never its PDF)
  work/mappings/    the mapping each table was read with

Access: ${man.graph?.access || "institution"}. The page never contributes it.

To rebuild graph.json with the command line (${man.tool}), in an empty folder outside the SCALE repository with
sources/ copied into it:

${steps}

The graph.json it writes is this one (sha256 ${man.graph?.sha256}).
`;
}
// A reading note of `scale add-source` as the page says it: the page's own controls in place of the command's options.
export function pageNote(text) {
  return String(text || "")
    .replace(/;?\s*give --sheet to choose another/, ": choose another sheet above")
    .replace(/;?\s*give --header-row to settle it/, ": set the header row above if it is another")
    .replace(/;?\s*give --range \(an A1 range\) to read another block/, ": only the first block of the sheet is read")
    .replace(/;?\s*the mapping records input\.(decimal|percent)/, "");
}
// The checklist's must items still missing on the added records, summed over the checklists.
export const checklistGaps = (res) => Object.values(res?.checklist || {}).reduce((n, c) => n + (c.missing || 0), 0);
// The cells a verification checked, over the datasets it read.
export const cellsChecked = (res) => (res?.verify?.datasets || []).reduce((n, d) => n + (d.cells || 0), 0);

// ------------------------------------------------------------------------------------------ the working copy
// IndexedDB, every call in try/catch: a private window, cleared site data or a blocked store leaves the page working,
// only without the copy (the page says so).
const DB = "scale-explorer", STORE = "private-graphs";
function idb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: "id" });
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
async function idbDo(mode, f) {
  const db = await idb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode), req = f(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}
export async function keep(rec) { try { await idbDo("readwrite", (s) => s.put(rec)); return true; } catch { return false; } }
export async function forget(id) { try { await idbDo("readwrite", (s) => s.delete(id)); return true; } catch { return false; } }
export async function kept() { try { return (await idbDo("readonly", (s) => s.getAll())) || []; } catch { return []; } }

// ------------------------------------------------------------------------------------------ the private graphs
export const MAX_BATCH = 10;                   // sources in one batch of Add a source
export const labs = new Map();                  // id -> { id, entry, text, doc, manifest, saved, kept, steps }
export const unsaved = () => [...labs.values()].filter((l) => !l.saved);

// The page's controller. env: { $, esc, rpc, status, showLab(lab, view), changed(), openCurateWith(file),
// openGraphFile(file), modelHelp(columns, roles, levels) (null without a key), paper() (the publication curated in
// this page, or null), download(name, blob) }.
export function addSource(env) {
  const { $, esc } = env;
  const lab = (fn, args) => env.rpc({ op: "lab", fn, args }).then((r) => { if (r && r.error) throw new Error(r.error); return r; });
  const st = { file: null, b64: "", det: null, into: "", staged: "", opts: {}, ticked: new Set(), res: null, busy: false, seq: 0, help: null };
  const prefs = (() => { try { return JSON.parse(localStorage.getItem("scale-add-prefs") || "{}"); } catch { return {}; } })();
  const savePrefs = (p) => { try { localStorage.setItem("scale-add-prefs", JSON.stringify(p)); } catch { /* per-viewer convenience only */ } };

  // a private graph's state after a change: its files from the worker, its working copy kept
  async function refresh(l, { saved = false } = {}) {
    const files = await lab("files", { gid: l.id });
    l.text = b64ToText(files["graph.json"]);
    l.doc = JSON.parse(l.text);
    l.manifest = JSON.parse(b64ToText(files["manifest.json"]));
    l.entry.title = l.doc.graph?.title || l.entry.title;
    l.saved = saved;
    l.kept = await keep({ id: l.id, title: l.entry.title, saved: l.saved, savedSha: l.savedSha || "", savedAt: l.savedAt || "", updated: new Date().toISOString(), files });
    env.changed();
    return files;
  }
  async function create({ title, project, contributor, access }) {
    const id = labId(title);
    await lab("new", { gid: id, project, contributor, access, title });
    const l = { id, entry: { id, title, from: "lab" }, saved: false, kept: false, savedSha: "" };
    labs.set(id, l);
    await refresh(l);
    savePrefs({ project, contributor });                     // never the access: public is chosen each time
    return l;
  }
  async function download(id) {
    const l = labs.get(id);
    if (!l) return;
    const files = await lab("files", { gid: id });
    const man = JSON.parse(b64ToText(files["manifest.json"]));
    const entries = Object.keys(files).sort().map((rel) => ({ name: `${id}/${rel}`, data: b64ToBytes(files[rel]) }));
    entries.push({ name: `${id}/README.txt`, data: new TextEncoder().encode(readme(man)) });
    env.download(`${id}.zip`, new Blob([zipStore(entries)], { type: "application/zip" }));
    l.savedSha = man.graph.sha256;
    l.savedAt = new Date().toISOString();
    await refresh(l, { saved: true });
  }
  // the working copies kept in this browser, back in the worker (a reload, a closed tab)
  async function restore() {
    const recs = await kept();
    for (const r of recs) {
      try {
        await lab("restore", { gid: r.id, contents: r.files });
        const text = b64ToText(r.files["graph.json"]);
        const doc = JSON.parse(text);
        labs.set(r.id, { id: r.id, entry: { id: r.id, title: r.title || doc.graph?.title || r.id, from: "lab" }, text, doc,
          manifest: JSON.parse(b64ToText(r.files["manifest.json"])), saved: !!r.saved, savedSha: r.savedSha || "", savedAt: r.savedAt || "", kept: true });
      } catch { /* a copy that does not read is left in storage, untouched */ }
    }
    if (recs.length) env.changed();
    return recs.length;
  }
  async function remove(id) {
    labs.delete(id);
    await forget(id);
    await lab("drop", { gid: id }).catch(() => {});
    env.changed();
  }

  // ---------------------------------------------------------------------------------------- the dialog: a batch
  // Up to MAX_BATCH sources at once (files of any kind, DOIs), all into ONE private graph, made once when the batch
  // starts: graphs and bundles merge first (no questions), then each table is confirmed in turn (or skipped), then each
  // paper is curated with the reader's key. A failure is that file's; the rest go on. Each file's outcome is kept and
  // the batch ends with one summary. While it runs, the files not yet added are noted in this browser, so that a reload
  // says which ones to add again.
  const step = $("add-step");
  const B = { items: [], dropped: [], running: false, gid: "", head: "", attest: false, seq: 0, token: 0 };
  const KIND = { table: "table", paper: "paper", graph: "graph", bundle: "bundle", download: "private-graph download", doi: "DOI", unknown: "unreadable" };
  const STATE = { reading: "reading…", waiting: "waiting", confirm: "needs your confirmation", curating: "curating", merging: "merging",
    added: "added", unchanged: "already there", failed: "failed", skipped: "skipped", "needs key": "needs your key" };
  function reset() {
    Object.assign(st, { file: null, b64: "", det: null, into: "", staged: "", opts: {}, ticked: new Set(), res: null, busy: false, help: null });
    Object.assign(B, { items: [], dropped: [], running: false, gid: "", head: "", attest: false, token: B.token + 1 });
    $("add-file").value = "";
    $("add-doi").value = "";
    $("add-file-name").textContent = `Drop files here (up to ${MAX_BATCH}), or choose them`;
    $("add-drop").classList.remove("has-file");
    $("add-pick").hidden = false;
    step.hidden = true;
    step.innerHTML = "";
  }
  function open() {
    if (!$("add-dialog").open) $("add-dialog").showModal();
    if (!B.running && B.done) { B.done = false; reset(); }
    else if (!B.running) renderPick();               // a key set meanwhile changes what the papers need
  }
  // a view: into the batch's stage while it runs (the list of files stays above it), else the whole step
  function show(html) {
    step.hidden = false;
    const a = document.activeElement, key = a && step.contains(a)
      ? (a.id ? `#${a.id}` : ["data-role", "data-assume", "data-confirm", "data-use"].map((k) => a.hasAttribute(k) ? `[${k}="${CSS.escape(a.getAttribute(k))}"]` : "").find(Boolean)) : "";
    const into = B.running && $("batch-stage") ? $("batch-stage") : step;
    into.innerHTML = (B.running ? B.head : "") + html;
    if (key) step.querySelector(key)?.focus();
  }
  function fail(msg) {
    if (B.running && st.item) {                     // the table's failure is the table's: the batch goes on
      show(`<p class="gx-err">${esc(msg)}</p><menu class="gx-add-menu"><button type="button" class="gx-btn gx-btn-primary" id="add-again">Go on with the next file</button></menu>`);
      $("add-again").onclick = () => finishTable("failed", msg);
      return;
    }
    show(`<p class="gx-err">${esc(msg)}</p><p><button type="button" class="gx-btn gx-btn-tiny" id="add-again">Start again</button></p>`);
    $("add-again").onclick = reset;
  }
  const busy = (msg) => show(`<p class="gx-subtle gx-add-busy">${esc(msg)}</p>`);

  // ---- choosing: files and DOIs into the list, each read by the Python router (what it is)
  async function addFiles(list) {
    if (B.running) return;
    const files = [...(list || [])];
    const room = Math.max(0, MAX_BATCH - B.items.length);
    B.dropped = [...B.dropped, ...files.slice(room).map((f) => f.name)];
    const fresh = files.slice(0, room).map((file) => ({ key: `i${++B.seq}`, name: file.name, file, size: file.size, kind: "", state: "reading" }));
    B.items.push(...fresh);
    renderPick();
    await Promise.all(fresh.map(readItem));
    renderPick();
  }
  async function readItem(it) {
    const token = B.token;
    try {
      if (/\.zip$/i.test(it.name)) {
        const pg = privateGraphIn(await readZip(new Uint8Array(await it.file.arrayBuffer())));
        Object.assign(it, pg ? { kind: "download", pg } : { kind: "bundle" }, { state: "waiting" });
        return;
      }
      it.b64 = bytesToB64(new Uint8Array(await it.file.arrayBuffer()));
      const r = await lab("detect", { name: it.name, data_b64: it.b64 });
      it.det = r.detected;
      it.kind = { table: "table", graph: "graph", document: "paper" }[it.det.route] || "unknown";
      it.state = "waiting";
    } catch (e) { Object.assign(it, { kind: it.kind || "unknown", state: "failed", why: e.message || String(e) }); }
    if (token === B.token) renderPick();
  }
  async function addDoi(text) {
    if (B.running) return;
    if (B.items.length >= MAX_BATCH) { B.dropped.push(text); renderPick(); return; }
    const it = { key: `i${++B.seq}`, name: text, kind: "", state: "reading" }, token = B.token;
    B.items.push(it);
    $("add-doi").value = "";
    renderPick();
    try {
      const det = (await lab("detect", { text })).detected;
      Object.assign(it, { name: det.doi, doi: det.doi, det, kind: "doi", state: "waiting" });
    } catch (e) { Object.assign(it, { kind: "unknown", state: "failed", why: e.message || String(e) }); }
    if (token === B.token) renderPick();
  }
  const count = (k) => B.items.filter((x) => x.kind === k && x.state !== "failed").length;
  function rows(editable) {
    return B.items.map((it, i) => `<li class="gx-batch-row is-${esc(it.state.replace(/\s/g, "-"))}" data-key="${esc(it.key)}">
      <span class="gx-batch-n">${i + 1}</span><span class="gx-batch-name" title="${esc(it.name)}">${esc(it.name)}</span>
      <span class="gx-chip gx-batch-kind">${esc(KIND[it.kind] || "…")}</span>
      <span class="gx-batch-state">${esc(STATE[it.state] || it.state)}${it.why ? `<span class="gx-subtle">: ${esc(it.why)}</span>` : it.what ? `<span class="gx-subtle">: ${esc(it.what)}</span>` : ""}</span>
      ${editable ? `<button type="button" class="gx-icon-btn" data-remove="${esc(it.key)}" aria-label="Remove ${esc(it.name)}" title="Remove it from the batch">×</button>` : ""}</li>`).join("");
  }
  // the batch before it runs: the list (each removable), what will happen to papers, the graph it goes into, Start
  function renderPick() {
    if (B.running) return;
    if (!B.items.length && !B.dropped.length) { step.hidden = true; step.innerHTML = ""; return; }
    step.hidden = false;
    if (!$("batch-list")) {
      step.innerHTML = `<p class="gx-subtle" id="batch-note"></p><ol class="gx-batch" id="batch-list" aria-label="The sources of this batch"></ol>
        <div id="batch-papers"></div>${intoForm(stemOf(B.items[0]?.name || "My private graph"))}
        <div class="gx-add-foot"><menu class="gx-add-menu"><button type="button" class="gx-btn" id="batch-clear">Clear</button><button type="button" class="gx-btn gx-btn-primary" id="batch-start">Add them</button></menu></div>`;
      wireInto();
      $("batch-clear").onclick = reset;
      $("batch-start").onclick = () => run();
    }
    $("add-file-name").textContent = `${B.items.length} of ${MAX_BATCH} sources chosen · drop or choose more`;
    $("add-drop").classList.toggle("has-file", !!B.items.length);
    $("batch-note").innerHTML = (B.dropped.length ? `<span class="gx-card-warn">At most ${MAX_BATCH} sources a batch: left out ${esc(B.dropped.join(", "))}. Add them in another batch.</span> ` : "")
      + `All of them go into one private graph. Graphs merge first, then each table is shown for you to confirm (or skip), then each paper is curated.`;
    $("batch-list").innerHTML = rows(true);
    $("batch-list").querySelectorAll("[data-remove]").forEach((b) => (b.onclick = () => { B.items = B.items.filter((x) => x.key !== b.dataset.remove); if (!B.items.length) B.dropped = []; renderPick(); }));
    const papers = count("paper"), key = env.key();
    $("batch-papers").innerHTML = !papers ? "" : key
      ? `<div class="gx-add-q"><p>${papers} ${papers === 1 ? "paper" : "papers"}: each one's text goes to <b>${esc(key)}</b> with your key, and to no
          one else; the model curates them one after another by SCALE's curate-corpus skill in this browser, every quote checked against the text. A PDF or text is never kept.</p>
        <label class="gx-check"><input type="checkbox" id="batch-attest"${B.attest ? " checked" : ""}> I have lawful access to ${papers === 1 ? "this paper" : "these papers"} (for example through my library) and curate ${papers === 1 ? "it" : "them"} to record the knowledge ${papers === 1 ? "it reports" : "they report"}: only what is derived from ${papers === 1 ? "it" : "them"} (findings, values and short cited quotes).</label></div>`
      : `<div class="gx-add-q gx-ask"><p>${papers} ${papers === 1 ? "paper" : "papers"}: curating needs your own API key, so ${papers === 1 ? "it is" : "they are"} marked <i>needs your key</i>; the tables and graphs still go in.</p>
        <button type="button" class="gx-btn gx-btn-tiny" id="batch-key">Set an API key</button></div>`;
    if ($("batch-attest")) $("batch-attest").onchange = () => { B.attest = $("batch-attest").checked; syncStart(); };
    if ($("batch-key")) $("batch-key").onclick = () => { $("add-dialog").close(); env.openKeyMenu(); };
    syncStart();
  }
  function syncStart() {
    const reading = B.items.some((x) => x.state === "reading"), any = B.items.some((x) => x.state === "waiting");
    const needAttest = count("paper") && env.key() && !B.attest;
    $("batch-start").disabled = reading || !any || !!needAttest;
    $("batch-start").title = reading ? "Still reading the files" : !any ? "Nothing to add" : needAttest ? "Tick the statement about the papers first" : "";
  }

  // ---- running: one private graph, made once; graphs, then tables one at a time, then papers
  const BATCH_NOTE = "scale-add-batch";
  function note() {                                // the files not yet added, for a reload to name (this browser only)
    if (!B.gid) return;
    try {
      const left = B.items.filter((x) => ["waiting", "confirm", "curating", "merging"].includes(x.state)).map((x) => x.name);
      if (left.length) localStorage.setItem(`${BATCH_NOTE}:${B.gid}`, JSON.stringify({ gid: B.gid, left }));
      else localStorage.removeItem(`${BATCH_NOTE}:${B.gid}`);
    } catch { /* the note is a convenience; the private graph is kept either way */ }
  }
  function renderRun() {
    if ($("batch-run-list")) $("batch-run-list").innerHTML = rows(false);
    note();
  }
  const settle = (it, state, extra = {}) => { Object.assign(it, { state }, extra); renderRun(); };
  async function run() {
    if (count("paper") && env.key() && !B.attest) { renderPick(); return; }   // the papers' statement, ticked first
    const download = B.items.find((x) => x.kind === "download" && x.state === "waiting");
    if (download && B.items.length === 1) return reopen(download.pg);     // a download alone: opened as the graph it was
    const c = chosen();
    B.running = true;
    $("add-pick").hidden = true;
    step.innerHTML = `<p class="gx-subtle" id="batch-into"></p><ol class="gx-batch" id="batch-run-list" aria-label="The sources of this batch"></ol>
      <div class="gx-batch-stage" id="batch-stage" aria-live="polite"></div>`;
    let l;
    try { l = await target(c); } catch (e) { B.running = false; return fail(e.message || String(e)); }
    B.gid = l.id;
    $("batch-into").innerHTML = `Into the private graph <b>${esc(l.entry.title)}</b>`;
    renderRun();
    for (const it of B.items.filter((x) => x.state === "waiting")) {   // what never goes in, said at once
      if (it.kind === "download") settle(it, "skipped", { why: "a private graph's download opens on its own: add it alone" });
      else if (it.kind === "doi") settle(it, "skipped", { why: `a page cannot fetch a paper by its DOI: give its PDF, or run scale add-source ${it.doi} --into graph.json` });
      else if (it.kind === "unknown") settle(it, "failed", { why: it.why || "not a file this page reads" });
    }
    for (const it of B.items.filter((x) => (x.kind === "graph" || x.kind === "bundle") && x.state === "waiting")) await mergeItem(l, it);
    const tables = B.items.filter((x) => x.kind === "table" && x.state === "waiting");
    for (const [i, it] of tables.entries()) await confirmTable(l, it, i + 1, tables.length);
    const papers = B.items.filter((x) => x.kind === "paper" && x.state === "waiting");
    if (papers.length && !env.key()) papers.forEach((it) => settle(it, "needs key", { why: "set your API key, then add it again" }));
    else for (const [i, it] of papers.entries()) await curateItem(l, it, i + 1, papers.length);
    B.running = false;
    note();
    summary(l);
  }
  async function mergeItem(l, it) {
    settle(it, "merging");
    B.head = "";
    show(`<p class="gx-subtle">Merging ${esc(it.name)}…</p>`);
    let staged = "";
    try {
      let b64 = it.b64, name = it.name;
      if (it.kind === "bundle") {                    // a knowledge-graph bundle, read here, merged as a graph
        const doc = await env.readBundleDoc(it.file);
        b64 = bytesToB64(new TextEncoder().encode(JSON.stringify(doc)));
        name = `${stemOf(it.name)}.json`;
      }
      const s = await lab("stage", { gid: l.id, name, data_b64: b64 });
      staged = s.file;
      const res = await lab("graph", { gid: l.id, file: s.file, write: true });
      if (!["merged", "unchanged"].includes(res.status)) throw new Error(res.status === "invalid" ? `it does not validate (${(res.validation?.first_errors || []).map((e) => e.message).slice(0, 2).join("; ")})` : `not merged (${res.status})`);
      await refresh(l);
      settle(it, res.status === "merged" ? "added" : "unchanged", { res, what: `${res.added || 0} records and links added`, why: "" });
    } catch (e) {
      if (staged) await lab("unstage", { gid: l.id, file: staged }).catch(() => {});
      settle(it, "failed", { why: e.message || String(e) });
    }
  }
  // a table: the mapping view (the same as for one file), with Skip; resolves when it is added, skipped or failed
  function confirmTable(l, it, n, of) {
    return new Promise((resolve) => {
      settle(it, "confirm");
      Object.assign(st, { item: it, token: ++B.seq, resolve, file: it.file, b64: it.b64, det: it.det, into: l.id, staged: "", opts: {}, ticked: new Set(), res: null, help: null, sheets: null });
      B.head = `<p class="gx-batch-head"><b>Table ${n} of ${of}</b>: ${esc(it.name)}</p>`;
      busy("Matching the table to the built-in mappings…");
      lab("stage", { gid: l.id, name: it.name, data_b64: it.b64 })
        .then((s) => { st.staged = s.file; return reread(); })
        .catch((e) => finishTable("failed", e.message || String(e)));
    });
  }
  function finishTable(state, why = "", res = null, token = st.token) {
    const it = st.item;
    if (!it || token !== st.token) return;           // a table already settled, or another's late answer
    st.item = null;
    st.token = 0;
    if (state !== "added" && state !== "unchanged" && st.staged) lab("unstage", { gid: st.into, file: st.staged }).catch(() => {});
    settle(it, state, res ? { res, what: `${res.records} records, ${res.results} results, ${cellsChecked(res)} cited cells match the file`, why: "" } : { why });
    const r = st.resolve;
    st.resolve = null;
    if (r) r();
  }
  async function curateItem(l, it, n, of) {
    settle(it, "curating");
    B.head = `<p class="gx-batch-head"><b>Paper ${n} of ${of}</b>: ${esc(it.name)}</p>`;
    show(`<div class="gx-curate-job" id="batch-job"></div>`);
    try {
      const c = await env.curateFile(it.file, $("batch-job"), l.doc?.graph?.stewardship || null);
      if (!c.check.ok) throw new Error(`its check still reports ${c.check.errors.length + (c.check.errors_not_shown || 0)} problems; it did not go in (curate it again)`);
      const res = await addPaper(l.id);
      settle(it, res.status === "merged" ? "added" : "unchanged", { res, what: `${res.added || 0} records and links (${c.title})`, why: "" });
    } catch (e) { settle(it, "failed", { why: e.message || String(e) }); }
  }
  // the batch's end: what each file added, what was skipped or failed and why, and the ways on
  function summary(l) {
    B.done = true;
    const added = B.items.filter((x) => x.state === "added" || x.state === "unchanged");
    const sum = (f) => added.reduce((n, x) => n + (x.res ? f(x.res) || 0 : 0), 0);
    const tile = (n, label) => `<div><b>${esc(Number(n).toLocaleString())}</b><span>${esc(label)}</span></div>`;
    const gaps = sum(checklistGaps);
    B.head = "";
    step.innerHTML = `<div class="gx-done-head"><span class="gx-done-mark">✓</span><div><b>${added.length} of ${B.items.length} sources added</b>
        <div class="gx-subtle">into ${esc(l.entry.title)}</div></div></div>
      <div class="gx-done-stats">${tile(sum((r) => r.records ?? r.added), "records (and merged links)")}${tile(sum((r) => r.results), "results")}${tile(sum(cellsChecked), "cited cells match the files")}${tile(gaps, "reporting gaps (checklist)")}</div>
      <ol class="gx-batch" id="batch-run-list" aria-label="What each source did">${rows(false)}</ol>
      ${gaps ? `<p class="gx-subtle">The reporting gaps are what the files do not say (a test's reaction, feed or electrolyte, for instance): add them when you know them, never typical values.</p>` : ""}
      <ul class="gx-subtle gx-batch-checks">
        ${B.items.some((x) => x.kind === "table" && x.res) ? "<li>Tables: every value cites a cell, checked against its file; the assumptions you confirmed are yours.</li>" : ""}
        ${B.items.some((x) => x.kind === "paper" && x.res) ? "<li>Papers: every quote checked against the paper's text by SCALE's check.</li>" : ""}
        ${B.items.some((x) => (x.kind === "graph" || x.kind === "bundle") && x.res) ? "<li>Merged graphs: their records come as they were; the data files their Datasets cite are not here, so those cells are not checked.</li>" : ""}
        <li>The graph follows the schema (validated after each source).</li></ul>
      <p class="gx-add-remind" role="alert"><b>Download it.</b> This private graph lives in this tab${l.kept ? " and in a working copy in this browser's storage, which the browser can clear" : " only (this browser keeps no copy)"}; the download is the durable copy, and the command line rebuilds it from the download.</p>
      <div class="gx-done-next">
        <button type="button" class="gx-btn gx-btn-primary" id="done-download">Download</button>
        <button type="button" class="gx-btn" id="done-graph">Explore the graph</button>
        <button type="button" class="gx-btn" id="done-analyze">Analyze</button>
        <button type="button" class="gx-btn" id="done-more">Add more sources</button>
      </div>`;
    step.hidden = false;
    $("add-pick").hidden = true;
    $("done-download").onclick = async () => { await download(l.id); $("done-download").textContent = "Download started"; };
    $("done-graph").onclick = () => { $("add-dialog").close(); env.showLab(l, "graph"); };
    $("done-analyze").onclick = () => { $("add-dialog").close(); env.showLab(l, "analyze"); };
    $("done-more").onclick = () => { B.done = false; reset(); };
    $("done-download").focus();
  }
  // a private graph's download, opened again: its folder back in the worker, as a private graph of this page
  async function reopen(pg) {
    busy("Restoring the private graph from its download…");
    try {
      let id = String(pg.manifest.id || "").match(/^[a-z0-9][a-z0-9-]{0,63}$/) ? pg.manifest.id : labId(pg.manifest.graph?.title);
      if (labs.has(id)) id = labId(pg.manifest.graph?.title || id);
      const g = pg.files["graph.json"], sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", g))].map((x) => x.toString(16).padStart(2, "0")).join("");
      if (pg.manifest.graph?.sha256 && sha !== pg.manifest.graph.sha256) throw new Error("its graph.json is not the one its manifest names (changed after the download?): not opened");
      const contents = {};
      for (const [rel, b] of Object.entries(pg.files)) contents[rel] = bytesToB64(b);
      await lab("restore", { gid: id, contents });
      const l = { id, entry: { id, title: pg.manifest.graph?.title || id, from: "lab" }, saved: true, kept: false, savedSha: pg.manifest.graph?.sha256 || "" };
      labs.set(id, l);
      await refresh(l, { saved: true });
      B.done = true;
      $("add-pick").hidden = true;
      show(`<p><b>${esc(l.entry.title)}</b> is open again as a private graph (${esc(l.doc.nodes.length)} records), from its download:
        add sources to it here, then download it again.</p>
        <div class="gx-done-next"><button type="button" class="gx-btn gx-btn-primary" id="done-graph">Explore the graph</button>
        <button type="button" class="gx-btn" id="done-analyze">Analyze</button><button type="button" class="gx-btn" id="done-more">Add sources to it</button></div>`);
      $("done-graph").onclick = () => { $("add-dialog").close(); env.showLab(l, "graph"); };
      $("done-analyze").onclick = () => { $("add-dialog").close(); env.showLab(l, "analyze"); };
      $("done-more").onclick = () => { B.done = false; reset(); };
    } catch (e) { fail(e.message || String(e)); }
  }

  // the graph a source goes into: a private graph of this page, or a new one (its stewardship: never public by default)
  function intoForm(defaultTitle) {
    const mine = [...labs.values()];
    const last = mine.length ? mine[mine.length - 1].id : "";
    return `<fieldset class="gx-add-into"><legend>Into</legend>
      <select id="add-into" aria-label="The private graph it goes into">${mine.map((l) => `<option value="${esc(l.id)}"${l.id === last ? " selected" : ""}>${esc(l.entry.title)} (${esc(env.levelLabel(l.entry))})</option>`).join("")}
        <option value=""${mine.length ? "" : " selected"}>A new private graph</option></select>
      <div class="gx-add-new" id="add-new"${mine.length ? " hidden" : ""}>
        <label>Title <input type="text" id="add-title-in" value="${esc(defaultTitle)}"></label>
        <div class="gx-add-two">
          <label>Project <input type="text" id="add-project" value="${esc(prefs.project || "lab:project/my-project")}" title="An id with a prefix: lab:project/…"></label>
          <label>Your lab <input type="text" id="add-contributor" value="${esc(prefs.contributor || "lab:org/my-lab")}" title="Your institution's id with a prefix: lab:org/…"></label>
        </div>
        <label>Who may read it (access) <select id="add-access">
          ${[["institution", "Private: your lab only (access institution)"], ["project", "Team: the project's members (access project)"], ["consortium", "Team: the project's institutions (access consortium)"], ["public", "Personal: anyone may read it (access public; only for data you may publish)"]]
            .map(([v, t]) => `<option value="${v}"${v === "institution" ? " selected" : ""}>${t}</option>`).join("")}</select></label>
        <p class="gx-subtle">Who may read the graph once you share it; in this browser it is yours alone.</p>
      </div></fieldset>`;
  }
  function wireInto(onChange = () => {}) {
    const sel = $("add-into");
    sel.onchange = () => { $("add-new").hidden = !!sel.value; onChange(); };
  }
  function chosen() {                               // the form's choice: a private graph's id, or a new one's settings
    const id = $("add-into")?.value;
    if (id) return { id };
    return { new: { title: $("add-title-in").value.trim() || "My private graph", project: $("add-project").value.trim(),
      contributor: $("add-contributor").value.trim(), access: $("add-access").value } };
  }
  async function target(c = chosen()) {             // the chosen private graph, made now if it is new
    return c.id ? labs.get(c.id) : create(c.new);
  }

  // ---------------------------------------------------------------------------------------- a table
  const stemOf = (name) => String(name || "").replace(/\.[^.]+$/, "");
  async function reread() {
    const my = ++st.seq;
    st.busy = true;
    $("add-map-state") ? ($("add-map-state").textContent = "Reading…") : busy("Matching the table to the built-in mappings…");
    try {
      const res = await lab("table", { gid: st.into, file: st.staged, opts: cleanOpts(st.opts), write: false });
      if (my !== st.seq) return;
      st.res = res;
      if (!st.sheets) st.sheets = (res.reading || []).map((x) => x.sheet).filter((x) => x);   // every sheet, as first read
      renderMapping();
    } catch (e) { if (my === st.seq) fail(e.message || String(e)); }
    st.busy = false;
  }
  const qOf = (res, id) => (res.questions || []).find((q) => q.id === id);
  function renderMapping() {
    const r = st.res, o = st.opts, l = labs.get(st.into);
    if (!r.template) {                               // nothing fits
      show(`<p class="gx-card-warn">No built-in mapping fits this table.</p>
        <p class="gx-muted">${esc((r.questions || [])[0]?.why || "")}</p>${readingControls(r)}
        <p class="gx-subtle">Or write a mapping for this layout on your computer (docs/data.md) and run <code>scale data add</code>.</p>
        <menu class="gx-add-menu"><button type="button" class="gx-btn gx-btn-primary" id="add-back">Skip this table</button></menu>`);
      wireReading();
      $("add-back").onclick = () => finishTable("skipped", "no built-in mapping fits it");
      return;
    }
    const roles = r.template_roles || {}, header = r.header || [];
    const asked = new Set((r.questions || []).map((q) => q.id));
    const bindQ = (role) => qOf(r, `bind:${role}`);
    const roleRows = Object.entries(roles).filter(([, x]) => x.kind !== "melt").map(([role, x]) => {
      const b = (r.roles || {})[role], q = bindQ(role), col = (o.bind || {})[role] || b?.column || "";
      const unit = b?.unit ? `${esc(b.unit)} <span class="gx-subtle">${b.unit_from_header ? "from the header" : "assumed"}</span>` : (x.default_unit ? `<span class="gx-subtle">${esc(x.default_unit)}</span>` : "");
      const opts = [`<option value=""${col ? "" : " selected"}>${col ? "(read by name)" : "— not in the file"}</option>`,
        ...header.map((h) => `<option value="${esc(h)}"${h === col ? " selected" : ""}>${esc(h)}</option>`)].join("");
      return `<tr class="${q ? "gx-ask" : ""}"><th scope="row">${esc(role.replace(/_/g, " "))}${x.required ? ' <span class="gx-subtle" title="required">*</span>' : ""}</th>
        <td><select data-role="${esc(role)}" aria-label="The column of ${esc(role)}">${opts}</select>${q ? `<div class="gx-add-why">${esc(q.why)}</div>` : ""}</td><td>${unit}</td></tr>`;
    }).join("");
    const meltRows = Object.entries(r.melt_columns || {}).map(([role, cs]) => `<tr><th scope="row">${esc(role.replace(/_/g, " "))}
      <div class="gx-subtle">wide: one result per column</div></th><td colspan="2"><ul class="gx-add-melt">${cs.map((c) =>
        `<li>${esc(c.column)} <span class="gx-subtle">→ ${esc(c.target || c.unresolved_target || c.id)}${c.unit ? `, ${esc(c.unit)}` : ""}</span></li>`).join("")}</ul></td></tr>`).join("");
    // the questions, each with its own control
    const qs = [];
    const rec = qOf(r, "recorded_at"), timeCol = (r.roles || {}).recorded_at?.column;
    if (timeCol) {                                   // a column holds the times: it alone is used, never a time typed in
      if (o.recorded_at) { delete o.recorded_at; setTimeout(reread, 0); }
      qs.push(`<div class="gx-add-q"><b>When were the data recorded?</b> Each row's time is read from the column <b>${esc(timeCol)}</b>.
        <label class="gx-check"><input type="checkbox" id="q-reported"${o.origin === "reported" ? " checked" : ""}> The table is from a publication (reported, not recorded)</label></div>`);
    } else if (!(rec || o.recorded_at || o.origin === "reported")) {   // no time asked: where the data come from, still
      qs.push(`<div class="gx-add-q"><label class="gx-check"><input type="checkbox" id="q-reported"> The table is from a publication (reported, not recorded or computed here)</label></div>`);
    } else {
      qs.push(`<div class="gx-add-q${rec ? " gx-ask" : ""}"><b>When were the data recorded?</b>
        <p class="gx-subtle">The time you give applies to every row; it is your local time (${esc(Intl.DateTimeFormat().resolvedOptions().timeZone || "")}), stored in UTC.</p>
        <div class="gx-add-two"><label>Recorded at (your local time)<input type="datetime-local" id="q-recorded" value="${esc(localOf(o.recorded_at))}"${o.origin === "reported" ? " disabled" : ""}></label>
        <label class="gx-check"><input type="checkbox" id="q-reported"${o.origin === "reported" ? " checked" : ""}> The table is from a publication (reported, not recorded)</label></div></div>`);
    }
    for (const q of (r.questions || []).filter((x) => x.id.startsWith("unit:"))) {
      qs.push(`<div class="gx-add-q gx-ask"><label class="gx-check"><input type="checkbox" data-confirm="${esc(q.id)}"> ${esc(q.why.split(";")[0])}; that unit is right</label>
        <p class="gx-subtle">Another unit: write it in the column's header in the file, as Name (unit), and add the file again.</p></div>`);
    }
    for (const id of (o.confirm || []).filter((c) => c.startsWith("unit:"))) {
      qs.push(`<label class="gx-check gx-add-q"><input type="checkbox" data-confirm="${esc(id)}" checked> The unit of ${esc(id.slice(5))} is right</label>`);
    }
    const aq = `assumptions:${r.template}`;
    if ((r.assumptions || []).length) {
      const done = (o.confirm || []).includes(aq);
      qs.push(`<fieldset class="gx-add-q${asked.has(aq) ? " gx-ask" : ""}"><legend>The mapping records these for every row: tick each that holds for your data</legend>
        ${r.assumptions.map((a, i) => `<label class="gx-check"><input type="checkbox" data-assume="${i}"${done || st.ticked.has(a) ? " checked" : ""}> ${esc(a)}</label>`).join("")}
        <p class="gx-subtle">One that does not hold for your data: do not add the file here. The file (a column saying it), or a mapping of your own on the command line, records what differs (docs/data.md).</p></fieldset>`);
    }
    const rq = qOf(r, "rights");
    if (rq || o.data_rights) {
      qs.push(`<label class="gx-add-q${rq ? " gx-ask" : ""}"><b>The rights of the table</b> <span class="gx-subtle">${esc(rq ? rq.why : "")}</span>
        <select id="q-rights"><option value="">choose…</option>${["own", "open", "public_domain", "restricted", "in_copyright"].map((c) => `<option${o.data_rights === c ? " selected" : ""}>${c}</option>`).join("")}</select></label>`);
    }
    for (const q of (r.questions || []).filter((x) => x.id.startsWith("fix:") || x.id === "mapping")) {
      qs.push(`<p class="gx-err gx-add-q">${esc(q.about)}: ${esc(q.why)}${q.candidates ? ` (${esc([].concat(q.candidates).slice(0, 6).join(", "))})` : ""}</p>`);
    }
    const sources = st.sources || [];
    const paper = env.paper();
    const linkOpts = [`<option value="">no: the table stands on its own</option>`,
      ...sources.map((s) => `<option value="${esc(s.id)}"${o.from_source === s.id ? " selected" : ""}>${esc(s.title)}</option>`),
      ...(paper && !sources.some((s) => s.id === paper.source_id) ? [`<option value="paper:">${esc(paper.title)} (curated in this page; added to the graph first)</option>`] : [])].join("");
    const sheets = (st.sheets || []).filter((x) => x && x !== r.sheet);
    const sample = (r.reading || []).find((x) => x.sheet === r.sheet) || (r.reading || [])[0] || {};
    const preview = header.length && sample.samples ? `<details class="gx-add-preview"><summary>The first rows, as read (${esc(sample.rows)} rows${sample.cells ? `, cells ${esc(sample.cells)}` : ""})</summary>
      <div class="gx-add-scroll"><table><thead><tr>${header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${[0, 1, 2].map((i) =>
        `<tr>${header.map((h) => `<td>${esc((sample.samples[h] || [])[i] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>` : "";
    const notRead = [...(r.unmapped_columns || []).map((c) => `the column ${c}`), ...sheets.map((x) => `the sheet ${x}`),
      ...(r.notes || []).filter((n) => /not read/.test(n)).map(pageNote)];
    const howRead = (r.notes || []).filter((n) => !/not read|^sheet /.test(n)).map(pageNote);
    const nq = (r.questions || []).length;
    const ready = r.status === "dry_run";
    const helpable = env.modelHelp && ((r.unmapped_columns || []).length || Object.keys(roles).some((k) => bindQ(k)));
    show(`<div class="gx-add-head"><div><b>Read as</b> <select id="map-template" aria-label="The kind of table">${(r.library || []).map((t) =>
        `<option value="${esc(t.id)}"${t.id === r.template ? " selected" : ""}>${esc(t.title)}</option>`).join("")}</select></div>
        <span class="gx-chip" title="How well the column names fit this kind of table (1: every required role found by name); it says nothing of the values">column-name match ${esc(r.score)}</span>
        ${(r.alternatives || []).filter((a) => a.complete).length ? `<span class="gx-subtle">others fit too: ${esc(r.alternatives.filter((a) => a.complete).map((a) => `${a.template} ${a.score}`).join(", "))}</span>` : ""}</div>
      ${readingControls(r)}${preview}
      <table class="gx-add-roles"><thead><tr><th>Role</th><th>Column</th><th>Unit</th></tr></thead><tbody>${roleRows}${meltRows}</tbody></table>
      ${qs.length ? `<section class="gx-add-qs" aria-label="Questions">${qs.join("")}</section>` : ""}
      ${notRead.length ? `<details class="gx-add-notread" open><summary>Not read (${notRead.length})</summary><ul>${notRead.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
      ${howRead.length ? `<details class="gx-add-notread"><summary>How it was read (${howRead.length})</summary><ul>${howRead.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
      <label>Link it to a publication in this graph (its data) <select id="map-source">${linkOpts}</select></label>
      ${o.from_source ? `<label>Where in the publication <input type="text" id="map-locator" value="${esc(o.locator || "")}" placeholder="Table 2"></label>` : ""}
      ${helpable ? `<div class="gx-add-help" id="map-help">${st.help ? st.help : `<button type="button" class="gx-btn gx-btn-tiny" id="map-ask">Ask your model about the columns not read</button>
        <span class="gx-subtle">Off unless you click: sends only those headers and three sample values each, after you approve.</span>`}</div>` : ""}
      <details class="gx-add-cli"><summary>The same on the command line</summary><pre class="gx-add-cmd">${esc(r.command)}</pre></details>
      <div class="gx-add-foot"><p class="gx-add-state ${ready ? "" : "gx-card-warn"}" id="add-map-state" role="status" aria-live="polite">${ready
        ? `Ready: ${esc(r.records)} records and ${esc(r.results)} results go into <b>${esc(l?.entry.title || "")}</b>; the ${esc(cellsChecked(r))} cells they cite match the file (${esc(r.verify?.status || "")}).`
        : `${nq} ${nq === 1 ? "question" : "questions"} left: answer ${nq === 1 ? "it" : "them"} above (only someone who knows the data can).`}</p>
      <menu class="gx-add-menu"><button type="button" class="gx-btn" id="add-back">Skip this table</button><button type="button" class="gx-btn gx-btn-primary" id="map-add"${ready ? "" : " disabled"}>Add to the graph</button></menu></div>`);
    wireReading();
    step.querySelectorAll("select[data-role]").forEach((s) => (s.onchange = () => {
      o.bind = { ...(o.bind || {}) };
      if (s.value) o.bind[s.dataset.role] = s.value; else delete o.bind[s.dataset.role];
      o.confirm = (o.confirm || []).filter((c) => !c.startsWith("unit:"));   // a unit confirmed was another column's
      if (r.sheet) o.sheet = r.sheet;                  // a column is the sheet's: the binding pins the sheet read
      reread();
    }));
    $("map-template").onchange = () => { o.template = $("map-template").value; delete o.bind; reconfirm(); reread(); };
    if ($("q-recorded")) $("q-recorded").onchange = () => { o.recorded_at = isoUTC($("q-recorded").value); reread(); };
    if ($("q-reported")) $("q-reported").onchange = () => {
      if ($("q-reported").checked) { o.origin = "reported"; delete o.recorded_at; } else delete o.origin;
      reread();
    };
    step.querySelectorAll("input[data-confirm]").forEach((c) => (c.onchange = () => {
      const set = new Set(o.confirm || []);
      c.checked ? set.add(c.dataset.confirm) : set.delete(c.dataset.confirm);
      o.confirm = [...set];
      reread();
    }));
    step.querySelectorAll("input[data-assume]").forEach((c) => (c.onchange = () => {
      const a = r.assumptions[Number(c.dataset.assume)];
      c.checked ? st.ticked.add(a) : st.ticked.delete(a);
      const all = r.assumptions.every((x) => st.ticked.has(x)), set = new Set(o.confirm || []);
      const had = set.has(aq);
      all ? set.add(aq) : set.delete(aq);
      o.confirm = [...set];
      if (all !== had) reread();
    }));
    if (o.confirm?.includes(aq)) r.assumptions.forEach((a) => st.ticked.add(a));
    if ($("q-rights")) $("q-rights").onchange = () => { o.data_rights = $("q-rights").value || undefined; reread(); };
    $("map-source").onchange = async () => {
      const v = $("map-source").value;
      if (v === "paper:") {
        $("add-map-state").textContent = "Adding the publication to the graph first…";
        try {
          const p = await addPaper(st.into);
          o.from_source = p.source_id;
        } catch (e) { $("add-map-state").textContent = e.message || String(e); return; }
      } else o.from_source = v || undefined;
      reread();
    };
    if ($("map-locator")) $("map-locator").onchange = () => { o.locator = $("map-locator").value.trim() || undefined; reread(); };
    if ($("map-ask")) $("map-ask").onclick = () => askModel(r);
    $("add-back").onclick = () => finishTable("skipped", "skipped by you");
    $("map-add").onclick = () => commit();
    lab("sources_of", { gid: st.into }).then((s) => {
      const had = JSON.stringify(st.sources || []);
      st.sources = s;
      if (JSON.stringify(s) !== had && st.res === r) renderMapping();
    }).catch(() => {});
  }
  // a reading changed (the kind of table, the sheet, the header row, the range): what was confirmed is asked again
  function reconfirm() {
    st.opts.confirm = [];
    st.ticked.clear();
  }
  function readingControls(r) {
    const sheets = st.sheets && st.sheets.length ? st.sheets : (r.reading || []).map((x) => x.sheet).filter((x) => x);
    const sq = qOf(r, "sheet");
    const found = (r.notes || []).map((n) => /header row was found, not given: row (\d+)/.exec(n)).find(Boolean);
    return `<div class="gx-add-reading">${sheets.length > 1 ? `<label class="${sq ? "gx-ask" : ""}">Sheet <select id="map-sheet">${sheets.map((s) =>
      `<option${s === (st.opts.sheet || r.sheet) ? " selected" : ""}>${esc(s)}</option>`).join("")}</select></label>` : ""}
      <label>Header row <input type="number" min="1" id="map-header" value="${esc(st.opts.header_row || "")}" placeholder="${found ? `found: ${found[1]}` : "found"}"></label>
      <label>Cells to read <input type="text" id="map-range" value="${esc(st.opts.range || "")}" placeholder="the first block (or A4:I12)" title="An A1 range: another block of the sheet"></label></div>`;
  }
  function wireReading() {
    if ($("map-sheet")) $("map-sheet").onchange = () => { st.opts.sheet = $("map-sheet").value; delete st.opts.bind; reconfirm(); reread(); };
    if ($("map-header")) $("map-header").onchange = () => { st.opts.header_row = $("map-header").value ? Number($("map-header").value) : undefined; reconfirm(); reread(); };
    if ($("map-range")) $("map-range").onchange = () => { st.opts.range = $("map-range").value.trim().toUpperCase() || undefined; reconfirm(); reread(); };
  }
  async function askModel(r) {
    const token = st.token;
    const roles = r.template_roles || {};
    const unbound = Object.keys(roles).filter((k) => roles[k].kind !== "melt" && !(r.roles || {})[k]);
    const sheet = (r.reading || []).find((x) => x.sheet === r.sheet) || (r.reading || [])[0] || {};
    const cols = (r.unmapped_columns || []).map((h) => ({ header: h, samples: (sheet.samples || {})[h] || [] }));
    try {
      $("map-help").innerHTML = `<span class="gx-subtle">Asking your model…</span>`;
      const s = await env.modelHelp(cols, unbound.map((k) => ({ role: k, ...roles[k] })), labs.get(st.into)?.doc);
      if (token !== st.token) return;               // the reader moved on to another table
      const pairs = Object.entries(s || {}).filter(([role, col]) => unbound.includes(role) && (r.header || []).includes(col));
      st.help = pairs.length ? `<span>Your model suggests (a suggestion only; choose it in the table):</span> ${pairs.map(([role, col]) =>
        `<button type="button" class="gx-btn gx-btn-tiny" data-use="${esc(role)}" data-col="${esc(col)}">${esc(role)} ← ${esc(col)}</button>`).join(" ")}`
        : `<span class="gx-subtle">Your model found no role among the columns not read.</span>`;
      renderMapping();
      step.querySelectorAll("button[data-use]").forEach((b) => (b.onclick = () => { st.opts.bind = { ...(st.opts.bind || {}), [b.dataset.use]: b.dataset.col }; if (r.sheet) st.opts.sheet = r.sheet; st.help = null; reread(); }));
    } catch (e) { $("map-help").innerHTML = `<span class="gx-err">${esc(e.message || String(e))}</span>`; }
  }
  async function commit() {
    const token = st.token;
    st.busy = true;
    $("map-add").disabled = true;
    if ($("add-back")) $("add-back").disabled = true;   // no skip while it is being added
    $("add-map-state").textContent = "Adding, validating and checking every cited cell against the file…";
    try {
      const gid = st.into;                          // captured: nothing the reader does meanwhile changes where it goes
      const res = await lab("table", { gid, file: st.staged, opts: cleanOpts(st.opts), write: true });
      if (res.status !== "added" && res.status !== "unchanged") throw new Error((res.questions || []).map((q) => q.why).join("; ") || `not added (${res.status})`);
      await refresh(labs.get(gid));
      st.busy = false;
      finishTable(res.status === "added" ? "added" : "unchanged", "", res, token);
    } catch (e) {
      st.busy = false;
      if (token !== st.token) return;
      $("add-map-state").textContent = e.message || String(e);
      $("map-add").disabled = false;
      if ($("add-back")) $("add-back").disabled = false;
    }
  }
  // after an addition: what went in, and the three ways on (explore, analyze, download), with the reminder
  function done(l, res) {
    const tile = (n, label) => `<div><b>${esc(typeof n === "number" ? n.toLocaleString() : n)}</b><span>${esc(label)}</span></div>`;
    const gaps = checklistGaps(res);
    show(`<div class="gx-done-head"><span class="gx-done-mark">✓</span><div><b>${res.status === "unchanged" ? "Already in the graph" : `Added${gaps ? `, with ${gaps} reporting gaps` : ""}`}</b>
        <div class="gx-subtle">${esc(st.file?.name || "")} → ${esc(l.entry.title)}${res.template ? ` · read as ${esc(res.template)}` : ""}</div></div></div>
      <div class="gx-done-stats">${res.records !== undefined ? tile(res.records, "records") + tile(res.results, "results") + tile(cellsChecked(res), res.verify?.status === "verified" ? "cited cells match the file" : `cited cells ${res.verify?.status || "checked"}`)
        : tile(res.added || 0, "records and links added") + tile(res.unchanged || 0, "already there") + tile(res.validation?.valid ? "valid" : "invalid", "structure")}${tile(gaps, "reporting gaps (checklist)")}</div>
      ${gaps ? `<details class="gx-add-gaps"><summary>The reporting gaps: what the file does not say</summary><ul>${Object.values(res.checklist || {}).flatMap((c) => c.first || []).slice(0, 12).map((g) =>
        `<li><code>${esc(String(g.record || "").split("/").slice(-2).join("/"))}</code> ${esc(g.message || "")}</li>`).join("")}</ul>
        <p class="gx-subtle">Add them when you know them (a test's reaction, feed or electrolyte, for instance), never typical values.</p></details>` : ""}
      ${res.warnings_rights ? `<p class="gx-card-warn">${esc(res.warnings_rights.replace(/ in \S+rights\.yaml/, " in rights.yaml"))}</p>` : ""}
      ${res.records === undefined && res.route === "graph" ? `<p class="gx-subtle">A merged graph's records keep their evidence; the data files its Datasets cite are not in this private graph, so their cells are not checked here (scale data verify, with the files).</p>` : ""}
      <p class="gx-subtle">The check is that every value cites a cell holding it, and that the graph follows the schema; the assumptions you confirmed are yours.</p>
      <p class="gx-add-remind" role="alert"><b>Download it.</b> This private graph lives in this tab${l.kept ? " and in a working copy in this browser's storage, which the browser can clear" : " only (this browser keeps no copy)"}; the download is the durable copy, and the command line rebuilds it from the download.</p>
      <div class="gx-done-next">
        <button type="button" class="gx-btn gx-btn-primary" id="done-download">Download</button>
        <button type="button" class="gx-btn" id="done-graph">Explore the graph</button>
        <button type="button" class="gx-btn" id="done-analyze">Analyze</button>
        <button type="button" class="gx-btn" id="done-more">Add another source</button>
      </div>`);
    $("add-pick").hidden = true;
    $("done-download").onclick = async () => { await download(l.id); $("done-download").textContent = "Downloaded"; };
    $("done-graph").onclick = () => { $("add-dialog").close(); env.showLab(l, "graph"); };
    $("done-analyze").onclick = () => { $("add-dialog").close(); env.showLab(l, "analyze"); };
    $("done-more").onclick = () => { B.done = false; reset(); };
    $("done-download").focus();
  }

  // the publication curated in this page, into a private graph (its graph and rights; never its PDF)
  async function addPaper(gid) {
    const p = env.paper();
    if (!p) throw new Error("no publication was curated in this page");
    const res = await lab("publication", { gid, slug: p.slug, about: p.about, write: true });
    if (!["merged", "unchanged"].includes(res.status)) throw new Error(`the publication was not added (${res.status})`);
    p.into = [...(p.into || []), gid];
    await refresh(labs.get(gid));
    return { ...res, source_id: p.source_id };
  }
  function paperStep() {
    const p = env.paper();
    reset();
    $("add-pick").hidden = true;
    show(`<p><b>${esc(p.title)}</b>, curated in this page: add its graph to a private graph, so that the tables you add
      link to it (its Source). Its PDF is not kept.</p>${intoForm(p.title)}<p class="gx-add-state" id="add-map-state"></p>
      <menu class="gx-add-menu"><button type="button" class="gx-btn gx-btn-primary" id="add-paper">Add it</button></menu>`);
    wireInto();
    $("add-paper").onclick = async () => {
      $("add-paper").disabled = true;
      $("add-map-state").textContent = "Adding…";
      try {
        const l = await target();
        const res = await addPaper(l.id);
        st.file = { name: p.title };
        done(l, res);
      } catch (e) { $("add-map-state").textContent = e.message || String(e); $("add-paper").disabled = false; }
    };
  }

  // ---------------------------------------------------------------------------------------- wiring
  $("add-file").onchange = () => { const fs = [...($("add-file").files || [])]; $("add-file").value = ""; if (fs.length) addFiles(fs); };
  for (const ev of ["dragenter", "dragover"]) $("add-drop").addEventListener(ev, (e) => { e.preventDefault(); $("add-drop").classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) $("add-drop").addEventListener(ev, () => $("add-drop").classList.remove("over"));
  $("add-drop").addEventListener("drop", (e) => { e.preventDefault(); const fs = [...(e.dataTransfer?.files || [])]; if (fs.length) addFiles(fs); });
  $("add-doi-go").onclick = () => { const t = $("add-doi").value.trim(); if (t) addDoi(t); };
  $("add-doi").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("add-doi-go").click(); } });
  // a tab with private graphs not downloaded, or a batch under way, asks before it closes
  window.addEventListener("beforeunload", (e) => {
    if (!unsaved().length && !B.running) return;   // (a batch under way, or a graph not downloaded: ask)
    e.preventDefault();
    e.returnValue = "";
  });
  // a batch a reload interrupted: the files it had not added yet, named on its private graph
  function interrupted() {
    try {
      for (const k of Object.keys(localStorage).filter((x) => x.startsWith(`${BATCH_NOTE}:`))) {
        const n = JSON.parse(localStorage.getItem(k) || "null");
        if (n && labs.has(n.gid) && Array.isArray(n.left) && n.left.length) labs.get(n.gid).interrupted = n.left.map(String).slice(0, MAX_BATCH);
      }
    } catch { /* none */ }
  }
  function dismissInterrupted(gid) {
    try { localStorage.removeItem(`${BATCH_NOTE}:${gid}`); } catch { /* as above */ }
    if (labs.has(gid)) delete labs.get(gid).interrupted;
    env.changed();
  }
  // more files for the dialog (the Curate dialog hands its files here)
  function take(files) { open(); if (B.done) { B.done = false; reset(); } addFiles(files); }
  return { open, take, restore: async () => { const n = await restore(); interrupted(); return n; }, download, remove, paperStep, dismissInterrupted, labs };
}
