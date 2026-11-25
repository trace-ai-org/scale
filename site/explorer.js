// The graph explorer. Pick a graph of this repository (data/graphs.json, written by `scale site build`) or open a file:
// the whole graph is drawn by a force layout (circle area by connectivity, colour by label), its labels and
// relationship types filter it, and a node's properties, evidence and neighbours show on the right. "Search by asking"
// lists the evidence that matches; signed in, the SCALE service answers in words by SCALE's query-graph skill, within
// its own budget. A reader may instead give their own API key (Anthropic, OpenAI or OpenRouter): the page then calls that
// provider's API itself (the key goes nowhere else) and runs the model's query plans here, by the same skill. Curating needs the service.
// Search, query plans and descriptions run SCALE's own code in a worker (worker.js).
import { readBundle } from "./bundle.js";
import { closedLevels, gate as egressGate, approve as egressApprove } from "./egress.js";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => DOMPurify.sanitize(marked.parse(String(s || "")));
const KIND_PALETTE = ["#5b8def", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#06b6d4", "#f43f5e", "#84cc16", "#0ea5e9", "#d946ef", "#fbbf24", "#22c55e"];
const EDGE_PALETTE = ["#7c3aed", "#0891b2", "#ea580c", "#16a34a", "#db2777", "#0284c7", "#ca8a04", "#9333ea", "#0d9488", "#dc2626", "#65a30d", "#2563eb"];
const NODE_R_MIN = 7, NODE_R_MAX = 40, NODE_R_STEP = 3.3;   // a node's radius from its own links only
const BIG_GRAPH = 400;            // past this, names hide when seen from far (arrowheads stay)
const HUGE_GRAPH = 1000;          // past this, the layout runs in slices
const CANVAS_GRAPH = 10000;       // past this, drawn on a canvas and laid out off the page's thread (layout.js)
const DRAW_DEFAULT = 30000;       // a bundle's window: how many records, until the reader sets another
const FAR_ZOOM = 0.6;
const LABELS_AUTO_MAX = 400;      // relationship names shown by default up to this many links
const PROVENANCE_KIND = "Source", PROVENANCE_EDGE = "REPORTED_IN";   // hidden until asked for, as the evidence is on each node

let G = null;                     // the graph: {entry, doc, byId, out, inn, labels, rels, sha, props}
let manifest = [];
let folded = false;               // the open graph's facts are folded under its row (its caret)
const kindColour = new Map(), edgeColour = new Map();
const S = {                       // what the canvas shows
  kind: null, rel: null, sources: false, hidden: new Set(), extra: new Set(), match: null,
  selected: null, layout: "force", linkLabels: "auto", viewMoved: false,
  sim: null, zoom: null, settleGen: 0, pos: new Map(), nodes: [], links: [], degree: new Map(), minDeg: 0, maxDeg: 0, draw: null,
};

// ------------------------------------------------------------------------------------------ the worker (SCALE)
const worker = new Worker("worker.js");
const pending = new Map();
let seq = 0, pythonReady = false;
worker.onmessage = (e) => {
  const m = e.data;
  if (m.status) {
    pythonReady = m.status === "ready";
    status(pythonReady ? (G && G.loaded ? ready() : "SCALE is ready in this browser.") : m.status);
    return;
  }
  const p = pending.get(m.id);
  if (!p) return;
  pending.delete(m.id);
  m.ok ? p.resolve(m.result) : p.reject(new Error(m.error));
};
const rpc = (msg) => new Promise((resolve, reject) => { const id = ++seq; pending.set(id, { resolve, reject }); worker.postMessage({ id, ...msg }); });
const scale = (tool, args) => rpc({ op: "call", tool, args: { ...args, ...(tool === "describe" ? {} : { path: G.file }) } });
const status = (t) => { $("status").textContent = t; };
const ready = () => "Search, queries and answers run in this browser.";

// ------------------------------------------------------------------------------------------ the graphs rail
const SCOPE = { "own-key": ["personal", "var(--green)"], service: ["personal", "var(--green)"], graphs: ["curated", "var(--green)"], examples: ["example", "var(--blue)"], domain: ["synthetic", "var(--violet)"] };
const scopeOf = (e) => SCOPE[(e.from || "").split("/")[0]] || ["file", "var(--subtle)"];
const CARET = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6 L15 12 L9 18"/></svg>`;

// ------------------------------------------------------------------------------------------ curating a source
const SERVICE = (window.SCALE_SERVICE_URL || "").replace(/\/$/, "");
let token = "", login = "";
// Signing in is one transaction, kept in this tab: a random nonce, where it was started (the toolbar, Curate, a search)
// and an expiry. The service carries the nonce and this page's address, its fragment (#g=…, the graph open) included,
// through GitHub and back; a token is taken only with the nonce this tab is waiting for, so a sign-in started anywhere
// else (someone's link, another tab, an abandoned one) never signs this tab in.
const SIGNIN = "scale-signin";
function signIn(from) {
  const n = Array.from(crypto.getRandomValues(new Uint8Array(18)), (b) => b.toString(16).padStart(2, "0")).join("");
  if (!store.set(SIGNIN, JSON.stringify({ n, from, exp: Date.now() + 15 * 60e3 }))) {
    const note = "Signing in needs this site's storage in your browser, which keeps the sign-in to this tab; allow it and try again.";
    return from === "curate" ? curateAccount(note) : accountMenu(note);
  }
  const back = location.origin + location.pathname + location.search + location.hash;
  location.assign(`${SERVICE}/auth/login?return=${encodeURIComponent(back)}&nonce=${n}`);
}
// every sign-in link carries data-signin="<where>"; caught before any menu stops the click
document.addEventListener("click", (e) => {
  const a = e.target.closest?.("[data-signin]");
  if (!a) return;
  e.preventDefault(); e.stopPropagation();
  signIn(a.dataset.signin);
}, true);
function readSignIn() {                          // the service returns from GitHub with #token=…&nonce=… (or why not)
  const p = new URLSearchParams(location.hash.slice(1));
  if (!p.has("token") && !p.has("signin")) return;
  const got = p.get("token"), why = p.get("signin"), n = p.get("nonce");
  for (const k of ["token", "signin", "login", "nonce"]) p.delete(k);
  history.replaceState(null, "", location.pathname + location.search + (p.toString() ? `#${p}` : ""));
  let tx = null;
  try { tx = JSON.parse(store.take(SIGNIN) || "null"); } catch { /* not a transaction */ }
  if (!tx || !n || tx.n !== n || !(tx.exp > Date.now())) {
    setTimeout(() => accountMenu("That sign-in was not started in this tab, or it took too long, so it was not used. Sign in again here."), 0);
    return;
  }
  if (got) { token = got; store.set("scale-token", token); }
  const note = why === "not-allowed" ? "This GitHub account is not admitted by the service yet; ask a maintainer to add it."
    : why ? "GitHub sign-in failed; try again." : "";
  if (tx.from === "curate") setTimeout(() => (why ? (openCurate(), curateAccount(note)) : openCurate()), 0);
  else if (why) setTimeout(() => accountMenu(note), 0);
}
function signOut() {
  token = ""; login = ""; store.set("scale-token", "");
  renderAccount();
  if (!$("account-menu").hidden) accountMenu();      // an open menu offers sign-in again, not the account just left
  if ($("curate-dialog").open) curateAccount();
}
// The toolbar's account: "Sign in" with GitHub, or the reader's avatar and login with a menu (what the service allows
// today, sign out). Without a service the button says why sign-in is not available yet.
let me = null;
async function renderAccount() {
  const btn = $("account-btn"), label = $("account-label");
  btn.hidden = !SERVICE;                          // nothing to sign in to without the service
  btn.classList.remove("is-signed-in");
  btn.querySelector("img")?.remove();
  btn.querySelector("svg").removeAttribute("hidden");
  if (!SERVICE || !token) { me = null; label.textContent = "Sign in"; btn.title = SERVICE ? "Sign in with GitHub" : "Sign-in opens once the SCALE service is running"; return; }
  try { me = await call("GET", "/me"); } catch { return; }       // a 401 signs out in call()
  login = me.login;
  const img = Object.assign(document.createElement("img"), { src: `https://github.com/${encodeURIComponent(me.login)}.png?size=64`, alt: "", className: "gx-avatar" });
  img.onerror = () => img.remove();
  btn.querySelector("svg").setAttribute("hidden", "");
  btn.prepend(img);
  btn.classList.add("is-signed-in");
  label.textContent = me.login;
  btn.title = `Signed in with GitHub as ${me.login}`;
}
function accountMenu(note = "") {
  const box = $("account-menu"), warn = note ? `<p class="gx-account-note gx-err">${esc(note)}</p>` : "";
  const open = `<p class="gx-account-note gx-subtle">Viewing, filtering and searching the graphs need no account.</p>`;
  if (!SERVICE) box.innerHTML = `<p class="gx-account-note">Sign-in with GitHub opens once the SCALE service is running; curating will
    need it.</p>${open}`;
  else if (!me) box.innerHTML = `${warn}<p class="gx-account-note">Curating a source needs a GitHub sign-in, as do contributing it to
    catal-commons and having questions answered in words. Free: the service reads only your GitHub login, never your
    repositories.</p>${open}
    <a class="gx-btn gx-btn-primary gx-account-go" href="#" role="button" data-signin="toolbar">Continue with GitHub</a>`;
  else box.innerHTML = `${warn}<p class="gx-account-note">Signed in as <b>${esc(me.login)}</b><br><span class="gx-subtle">${
    me.tokens_left_today === 0 ? "The service's budget for today is spent." : "Curating and questions are open to you today."}</span></p>
    <a class="gx-menu-item" href="https://github.com/${encodeURIComponent(me.login)}" target="_blank" rel="noopener">Your GitHub profile</a>
    <button class="gx-menu-item" type="button" id="account-board">Contributors to catal-commons</button>
    <button class="gx-menu-item" type="button" id="account-signout">Sign out</button>`;
  $("account-signout")?.addEventListener("click", signOut);
  $("account-board")?.addEventListener("click", () => $("board-open")?.click());
  box.hidden = false;
  $("account-btn").setAttribute("aria-expanded", "true");
}
async function call(method, path, body, headers = {}) {
  if (!SERVICE) throw new Error("the curation service is not configured (site/config.js)");
  const h = { ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  if (body !== undefined && !(body instanceof Blob)) { h["Content-Type"] = "application/json"; body = JSON.stringify(body); }
  const r = await fetch(SERVICE + path, { method, headers: h, body });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { signOut(); }
  if (!r.ok) throw Object.assign(new Error(data.error || `the service answered ${r.status}`), { status: r.status });
  return data;
}
// Who curates: the reader's own key, in this browser, when one is set (as questions are answered); else the service,
// with a GitHub sign-in. The dialog's terms say which, for the provider and model that will read the source.
const SERVICE_TERMS = $("curate-terms").innerHTML;
const curateMode = () => ownKey ? "own" : SERVICE ? "service" : "";
function curateTerms() {
  const own = curateMode() === "own";
  $("curate-terms").innerHTML = own ? `
    <li><b>Private.</b> The PDF is read in your browser, and the graph opens here for you alone.</li>` : SERVICE_TERMS;
  $("curate-details").hidden = own;
}
async function curateAccount(note = "") {
  const el = $("curate-account");
  const warn = note ? `<span class="gx-err">${esc(note)}</span>` : "";
  curateTerms();
  if (curateMode() === "own") {
    el.innerHTML = warn;                                  // the key is the toolbar's: nothing to say about it here
    syncCurate();
    return;
  }
  if (!SERVICE) {
    el.innerHTML = `<span class="gx-warn">Set your API key to curate in this browser.</span>
      <button type="button" class="gx-btn gx-btn-tiny gx-btn-primary" id="curate-key">Set an API key</button>${warn}`;
    $("curate-key").onclick = () => { $("curate-dialog").close(); openOwnKeyMenu(); };
    syncCurate();
    return;
  }
  if (!token) {
    el.innerHTML = `<a class="gx-btn gx-btn-primary" id="curate-signin" href="#" role="button" data-signin="curate">Sign in with GitHub</a>
      <span class="gx-subtle">Free; the service needs only your login.</span>${warn}`;
    syncCurate();
    return;
  }
  try {
    const me = await call("GET", "/me");
    login = me.login;
    el.innerHTML = `<span>Signed in as <b>${esc(me.login)}</b></span><span class="gx-subtle">· ${esc(me.model || "")}</span>`
      + `${me.tokens_left_today === 0 ? `<span class="gx-err">· the service's budget for today is spent</span>` : ""}`
      + `<button type="button" class="gx-btn gx-btn-tiny" id="curate-signout">Sign out</button>${warn}`;
    $("curate-signout").onclick = signOut;
    syncCurate();
  } catch { /* curateAccount ran again on 401 */ }
}
async function openCurate() {
  if (!curating && $("curate-dialog").classList.contains("is-done")) curateReset();
  if (!$("curate-dialog").open) $("curate-dialog").showModal();
  curateAccount();
  if (curateMode() !== "service") { $("curate-schema").textContent = ""; return; }
  try {
    const h = await call("GET", "/health");
    $("curate-schema").textContent = `Curated under SCALE Catalysis, schema scale-catal v${h.schema_version} (scale-catal ${h.scale_catal}).`;
    $("curate-keep-row").hidden = !h.keeps_texts;
    if (h.contact) $("curate-contact").innerHTML = `A rights holder can ask for a source's records or kept copy to be removed: <a href="mailto:${esc(h.contact)}">${esc(h.contact)}</a>.`;
    if (h.keeps_texts && token) showKept();
    // with an inbox, Claude Code curates on the service's own computer, in turn with the other sources sent
    if (h.inbox) $("curate-time").textContent = "Claude Code curates each source on the service's computer, in turn with the others sent, which can take an hour; you can close the page and come back.";
    if (h.inbox && token) showInbox();
  } catch (e) { $("curate-schema").innerHTML = `<span class="gx-err">The service did not answer: ${esc(e.message)}</span>`; }
}
$("curate-open").onclick = () => openCurate();
// The leaderboard: catal-commons' contributors scored by tools/commons.py (SCORING). The service's copy carries each
// source's reach (how often it answered other people); the site's copy (data/leaderboard.json) is the same without it.
let boardData = null, boardTab = "contributors";
const BOARD_TOP = 20;
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
function boardRow(r) {
  const lv = r.level || {};
  const next = lv.next ? `<div class="gx-subtle">${Number(lv.to_next).toLocaleString()} to ${esc(lv.next)}</div>` : "";
  const sources = (r.contributions || []).map((c) => `<tr><td>${esc(c.source)}</td><td>${esc(c.added)}</td><td>${Number(c.triples).toLocaleString()}</td>
    <td>${Number(c.reach).toLocaleString()}</td><td>${pct(c.completeness)}</td><td>${Number(c.score).toLocaleString()}</td></tr>`).join("");
  const mine = login && r.github.toLowerCase() === login.toLowerCase();
  return `<tr class="gx-board-row${mine ? " is-me" : ""}" tabindex="0"><td>${esc(r.rank)}</td>
    <td><a href="https://github.com/${esc(r.github)}" target="_blank" rel="noopener"><img src="https://github.com/${esc(r.github)}.png?size=40" alt="" loading="lazy">${esc(r.github)}</a>
      <span class="gx-level gx-level-${esc((lv.name || "").toLowerCase().replace(/ /g, "-"))}">${esc(lv.name || "")}</span>${mine ? ` <span class="gx-subtle">you</span>` : ""}</td>
    <td class="gx-num"><b>${Number(r.score).toLocaleString()}</b></td><td class="gx-num">${Number(r.triples).toLocaleString()}</td>
    <td class="gx-num">${boardData.with_reach ? Number(r.reach).toLocaleString() : "–"}</td><td class="gx-num">${pct(r.completeness)}</td><td class="gx-num">${esc(r.sources)}</td></tr>
    <tr class="gx-board-more" hidden><td></td><td colspan="6">${next}<table class="gx-board-sources"><tr><th>Source</th><th>Merged</th><th>Triples</th><th>Reach</th><th>Complete</th><th>Score</th></tr>${sources}</table></td></tr>`;
}
function drawBoard() {
  const rows = boardData[boardTab] || [];
  if (!rows.length) {
    $("board").innerHTML = `<p class="gx-muted">${boardTab === "recent" ? "Nothing merged in the last 90 days yet." : "No contributions are merged yet: the first source you contribute puts you at the top."}</p>`;
    return;
  }
  const top = rows.slice(0, BOARD_TOP);
  const me = login && rows.find((r, i) => i >= BOARD_TOP && r.github.toLowerCase() === login.toLowerCase());
  $("board").innerHTML = `<table class="gx-board-table"><tr><th>#</th><th>Contributor</th><th class="gx-num">Score</th><th class="gx-num">Triples</th>
    <th class="gx-num" title="Distinct people and days whose questions it answered">Reach</th><th class="gx-num" title="Checklist items and evidence found">Complete</th><th class="gx-num">Sources</th></tr>
    ${top.map(boardRow).join("")}${me ? `<tr class="gx-board-gap"><td colspan="7">…</td></tr>${boardRow(me)}` : ""}</table>`
    + (rows.length > BOARD_TOP && !me ? `<p class="gx-subtle">${rows.length - BOARD_TOP} more contributors.</p>` : "")
    + (boardData.with_reach ? "" : `<p class="gx-subtle">Reach is counted by the curation service; it shows here once the service is set up.</p>`);
  $("board").querySelectorAll(".gx-board-row").forEach((tr) => {
    const toggle = (e) => { if (e.target.closest("a")) return; tr.nextElementSibling.hidden = !tr.nextElementSibling.hidden; tr.classList.toggle("is-open"); };
    tr.onclick = toggle;
    tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e); } };
  });
}
document.querySelectorAll(".gx-board-tab").forEach((b) => (b.onclick = () => {
  boardTab = b.dataset.board;
  document.querySelectorAll(".gx-board-tab").forEach((x) => x.classList.toggle("is-on", x === b));
  if (boardData) drawBoard();
}));
$("board-open").onclick = async () => {
  $("board-dialog").showModal();
  try {
    try { boardData = await call("GET", "/leaderboard"); } catch { boardData = await (await fetch("data/leaderboard.json")).json(); }
    const s = boardData.scoring || {};
    $("board-how").innerHTML = Object.entries(s).map(([k, v]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(v)}</dd>`).join("");
    drawBoard();
  } catch { $("board").innerHTML = `<p class="gx-muted">The leaderboard is published with the site.</p>`; }
};
$("board-curate").onclick = () => { $("board-dialog").close(); openCurate(); };
// The sources the service keeps because this reader agreed, each with a way to withdraw.
async function showKept() {
  try {
    const r = await call("GET", "/kept");
    $("curate-kept").innerHTML = (r.kept || []).length ? `<details><summary>Sources kept with your agreement (${r.kept.length})</summary><ul>${r.kept.map((k) =>
      `<li>${esc(k.title || k.doi || k.sha256.slice(0, 12))} <button type="button" class="gx-btn gx-btn-tiny" data-forget="${esc(k.sha256)}">Withdraw</button></li>`).join("")}</ul></details>` : "";
    $("curate-kept").querySelectorAll("[data-forget]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { await call("POST", "/kept/forget", { sha256: b.dataset.forget }); showKept(); } catch (e) { b.disabled = false; alert(e.message); }
    }));
  } catch { $("curate-kept").innerHTML = ""; }
}
// The sources this reader sent to the inbox, each with its state; a curated one opens again, with its receipt.
async function showInbox() {
  try {
    const jobs = ((await call("GET", "/inbox")).jobs || []).slice(0, 10);
    const open = jobs.some((j) => !["done", "failed"].includes(j.state));
    $("curate-inbox").innerHTML = jobs.length ? `<details${open ? " open" : ""}><summary>Your sources in the inbox (${jobs.length})</summary><ul>${jobs.map((j) =>
      `<li>${esc(j.filename || j.doi || j.id.slice(0, 8))} · ${esc(j.state)} <button type="button" class="gx-btn gx-btn-tiny" data-follow="${esc(j.id)}">${j.state === "done" ? "Open" : "Follow"}</button></li>`).join("")}</ul></details>` : "";
    $("curate-inbox").querySelectorAll("[data-follow]").forEach((b) => (b.onclick = () => { $("curate-job").hidden = false; followJob(b.dataset.follow, true); }));
  } catch { $("curate-inbox").innerHTML = ""; }
}
// One drop zone: the source's PDF.
// Curate is grey until there is something to curate: a PDF, the statement ticked, a key (or a service sign-in), and
// no curation under way.
function syncCurate() {
  const mode = curateMode();
  $("curate-start").disabled = curating || !$("curate-pdf").files[0] || !$("curate-attest").checked
    || !mode || (mode === "service" && !token);
}
$("curate-attest").addEventListener("change", syncCurate);
const pdfName = () => { const f = $("curate-pdf").files[0]; $("curate-pdf-name").textContent = f ? f.name : "Drop a PDF here, or choose one"; $("curate-drop").classList.toggle("has-file", !!f); $("curate-attest-row").hidden = !f; syncCurate(); };
$("curate-pdf").onchange = pdfName;
for (const ev of ["dragenter", "dragover"]) $("curate-drop").addEventListener(ev, (e) => { e.preventDefault(); $("curate-drop").classList.add("over"); });
for (const ev of ["dragleave", "drop"]) $("curate-drop").addEventListener(ev, () => $("curate-drop").classList.remove("over"));
$("curate-drop").addEventListener("drop", (e) => {
  e.preventDefault();
  const f = [...(e.dataTransfer?.files || [])].find((x) => x.type === "application/pdf" || /\.pdf$/i.test(x.name));
  if (f) { const dt = new DataTransfer(); dt.items.add(f); $("curate-pdf").files = dt.files; pdfName(); }
});
async function startCuration(again = false) {
  if (curateMode() === "own") return curateOwnKey();
  const job = $("curate-job");
  job.hidden = false;
  try {
    if (!token) throw new Error("sign in with GitHub first");
    const f = $("curate-pdf").files[0];
    if (!f) throw new Error("give the source's PDF");
    if (!$("curate-attest").checked) throw new Error("tick the statement that you have lawful access to this PDF");
    const keep = !$("curate-keep-row").hidden && $("curate-keep").checked;
    const q = new URLSearchParams({ ...(again ? { again: "1" } : {}), ...(keep ? { keep: "1" } : {}), filename: f.name, attest: "1" });
    const res = await call("POST", `/jobs?${q}`, f, { "Content-Type": "application/pdf" });
    if (res.known) { showKnown(res.known); return; }
    curating = true; syncCurate();
    await followJob(res.job, !!res.inbox);
  } catch (e) { job.innerHTML = `<span class="gx-err">${esc(e.message)}</span>`; }
  curating = false; syncCurate();
}
$("curate-start").onclick = () => startCuration();
// Curating with the reader's own key, all in this browser: pdf.js reads the PDF's text layer page by page, SCALE starts
// the source's folder in Pyodide (scale source start), and the model fills the graph by
// the curate-corpus skill with scale.curation_tools (the service's own tools), through modelLoop, until it calls done.
// The graph then opens on the canvas, as one the service curated would, without a receipt: it is the reader's.
const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/";
const PDFJS_SRI = { "pdf.min.js": "sha384-/1qUCSGwTur9vjf/z9lmu/eCUYbpOTgSjmpbMQZ1/CtX2v/WcAIKqRv+U1DUCG6e",
  "pdf.worker.min.js": "sha384-SnzOobpRMLXZ52iJvZm/C0fYw0OQemTXzTjIsdsfMcrCtCEe9qgzxTd3RSklO5x2" };
let pdfjs = null;
function loadPdfjs() {                                     // loaded when first needed, each file checked against its digest
  pdfjs ||= new Promise((resolve, reject) => {
    const el = Object.assign(document.createElement("script"), { src: PDFJS + "pdf.min.js", integrity: PDFJS_SRI["pdf.min.js"], crossOrigin: "anonymous" });
    el.onload = async () => {
      try {
        const w = await fetch(PDFJS + "pdf.worker.min.js", { integrity: PDFJS_SRI["pdf.worker.min.js"] });
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = URL.createObjectURL(new Blob([await w.text()], { type: "text/javascript" }));
        resolve(window.pdfjsLib);
      } catch (err) { reject(err); }
    };
    el.onerror = () => reject(new Error("pdf.js could not be loaded"));
    document.head.append(el);
  });
  pdfjs.catch(() => { pdfjs = null; });
  return pdfjs;
}
async function pdfText(file) {                            // "[page N]" and the page's text, as scale source start writes it
  const lib = await loadPdfjs();
  const pdf = await lib.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
  let text = "", chars = 0;
  for (let i = 1; i <= pdf.numPages; i++) {
    const c = await (await pdf.getPage(i)).getTextContent();
    const page = c.items.map((it) => it.str + (it.hasEOL ? "\n" : "")).join("").replace(/[ \t]+\n/g, "\n").trim();
    chars += page.length;
    text += `[page ${i}]\n${page}\n\n`;
  }
  return { text, pages: pdf.numPages, chars };
}
let curating = false, finishNow = false, commonsVersion = "";   // the schema catal-commons pins: curated under it
const FINISH_UP = "The reader asked you to finish now. Write no more new records: run check, fix what it reports, then call done "
  + "saying what was curated and what was left out.";
// Where a curation is, and about how long it has left. Four stages; the model's stage advances with the pages it has
// read (most of the work), then with each check it runs; the time left is the elapsed time scaled by what is left,
// a rough estimate from the page count until enough is done to measure.
const STAGES = [["read", "Read the PDF"], ["prepare", "Prepare SCALE in your browser"], ["curate", "Curate: read every page and write records"],
  ["check", "Check every quote against the PDF and fix"]];
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
function curateProgress(job) {
  const st = { stage: "read", detail: "", pages: 0, seen: new Set(), records: 0, checks: 0, errors: null, t0: Date.now(), log: [], frac: 0 };
  job.hidden = false;
  job.innerHTML = `<div class="gx-cj-head"><b id="cj-title">Curating</b><span class="gx-subtle" id="cj-time"></span></div>
    <div class="gx-cj-bar"><span id="cj-bar"></span></div>
    <ol class="gx-cj-steps" id="cj-steps"></ol>
    <div class="gx-cj-detail" id="cj-detail"></div>
    <details class="gx-cj-log"><summary id="cj-logsum">Activity</summary><ol id="cj-log"></ol></details>
    <div id="cj-end"></div>
    <button type="button" class="gx-btn gx-btn-tiny" id="curate-finish" title="Write no more records: check, fix and finish" hidden>Finish now</button>`;
  $("curate-finish").onclick = (e) => { finishNow = true; e.target.disabled = true; e.target.textContent = "Finishing…"; st.note("Asked the model to check, fix and finish"); };
  const order = STAGES.map(([k]) => k);
  st.frac = () => {
    if (st.stage === "read") return 0.02;
    if (st.stage === "prepare") return 0.06;
    if (st.stage === "curate") return 0.1 + 0.6 * (st.pages ? st.seen.size / st.pages : 0);
    if (st.stage === "check") return Math.min(0.97, 0.75 + 0.2 * (1 - 0.6 ** st.checks));
    return 1;
  };
  st.render = () => {
    const i = order.indexOf(st.stage), f = st.frac(), el = (Date.now() - st.t0) / 1000;
    $("cj-steps").innerHTML = STAGES.map(([k, label], j) =>
      `<li class="${j < i || st.stage === "done" ? "done" : j === i ? "now" : ""}">${esc(label)}</li>`).join("");
    $("cj-bar").style.width = `${Math.round(f * 100)}%`;
    let left = "";
    if (st.stage === "done" || st.stage === "failed") left = "";
    else if (f > 0.15 && el > 30) left = ` · about ${Math.max(1, Math.round(el * (1 - f) / f / 60))} min left`;
    else if (st.pages) left = ` · usually ${Math.max(2, Math.round(st.pages * 0.7))}–${Math.max(5, Math.round(st.pages * 2))} min for ${st.pages} pages`;
    $("cj-time").textContent = `${mmss(el)} elapsed${left}`;
    $("cj-detail").textContent = st.detail;
  };
  st.note = (line) => { st.log.push(`${mmss((Date.now() - st.t0) / 1000)} ${line}`); $("cj-log").innerHTML = st.log.map((l) => `<li>${esc(l)}</li>`).join(""); $("cj-logsum").textContent = `Activity (${st.log.length})`; };
  st.go = (stage, detail) => {
    st.stage = stage; st.detail = detail; st.note(detail); st.render();
    if (stage === "curate" && $("curate-finish")) $("curate-finish").hidden = false;   // the model's stages only
  };
  st.timer = setInterval(st.render, 1000);
  st.end = (title, html) => {
    clearInterval(st.timer);
    $("cj-title").textContent = title;
    $("curate-finish")?.remove();
    $("cj-end").innerHTML = html;
    st.render();
  };
  // what each tool the model calls says about where it is
  st.tool = (tool, args, result) => {
    if (tool === "read_pages") {
      for (let n = Number(args.first); n <= Math.min(Number(args.last), st.pages); n++) st.seen.add(n);
      st.detail = `Read ${st.seen.size} of ${st.pages} pages · ${st.records} records written`;
    } else if (tool === "upsert") {
      try { const r = JSON.parse(result); st.records = r.nodes + r.links; } catch { /* an error, said to the model */ }
      st.detail = `Read ${st.seen.size} of ${st.pages} pages · ${st.records} records written`;
    } else if (tool === "check") {
      st.checks++; st.stage = "check";
      try { const r = JSON.parse(result); st.errors = r.errors.length + (r.errors_not_shown || 0); } catch { /* as above */ }
      st.detail = st.errors ? `Check ${st.checks}: ${st.errors} problems to fix · ${st.records} records` : `Check ${st.checks}: no errors · ${st.records} records`;
    } else if (tool === "describe") st.detail = "Reading the schema";
    else if (tool === "remove") st.detail = `Removing records · ${st.records} records`;
    st.note(st.detail);
    st.render();
  };
  return st;
}
// Done: the form gives way to what was curated, in a few numbers, and what to do next: explore the graph (it is open
// on the canvas, and questions now go to it), contribute it to catal-commons, or curate another source.
function curateDone({ doc, check, r, pages, secs, title, log }) {
  const by = {};
  for (const n of doc.nodes) by[n.label] = (by[n.label] || 0) + 1;
  const skip = new Set(["Source", "Institution", "Project", "Person"]);
  const kinds = Object.entries(by).filter(([l]) => !skip.has(l)).sort((x, y) => y[1] - x[1]).slice(0, 8);
  const evidenced = [...doc.nodes, ...doc.links].filter((x) => (x.props?.evidence || []).length).length;
  const problems = check.ok ? 0 : check.errors.length + (check.errors_not_shown || 0);
  const tile = (n, label) => `<div><b>${Number(n).toLocaleString()}</b><span>${esc(label)}</span></div>`;
  $("curate-dialog").classList.add("is-done");
  $("curate-job").hidden = false;
  $("curate-job").innerHTML = `<div class="gx-done-head"><span class="gx-done-mark">✓</span><div><b>Curated</b><div class="gx-subtle">${esc(title)}</div></div></div>
    <div class="gx-done-stats">${tile(doc.nodes.length, "records")}${tile(doc.links.length, "links")}${tile(evidenced, "with evidence")}${tile(pages, "pages read")}</div>
    <div class="gx-done-kinds">${kinds.map(([l, n]) => `<span class="gx-chip">${esc(l)} <b>${n}</b></span>`).join("")}</div>
    <p class="${problems ? "gx-card-warn" : "gx-subtle"}">${problems ? `${problems} problems the check still reports: the graph is open, and a reviewer sees them.` : "The check passes: every quote was found in the PDF."}
      Took ${mmss(secs)}${r.stopped ? `; ${esc(r.stopped)}` : ""}.</p>
    <details><summary>What the model curated</summary><div class="gx-done-summary">${idLinks(md(r.stopInput?.summary || r.text || ""))}</div>
      ${problems ? `<p><b>Problems left</b></p><ul>${check.errors.slice(0, 20).map((e) => `<li>${esc(typeof e === "string" ? e : (e.record ? `${e.record}: ` : "") + (e.message || JSON.stringify(e)))}</li>`).join("")}</ul>` : ""}
      <p class="gx-subtle">${esc(accountLine(r))}.</p></details>
    <details><summary>Activity (${log.length})</summary><ol>${log.map((l) => `<li>${esc(l)}</li>`).join("")}</ol></details>
    <div class="gx-done-next">
      <button type="button" class="gx-btn gx-btn-primary" id="done-explore">Explore the graph</button>
      <button type="button" class="gx-btn" id="done-contrib">Contribute to catal-commons</button>
      <button type="button" class="gx-btn" id="done-again">Curate another</button>
    </div>`;
  $("done-explore").onclick = () => {
    $("curate-dialog").close();
    $("search-input").placeholder = "Ask a question about this graph...";
    $("search-input").focus();
  };
  $("done-contrib").onclick = () => { $("curate-dialog").close(); contribute(); };
  $("done-again").onclick = () => curateReset();
  $("done-explore").focus();
}
function curateReset() {                         // the form again, empty
  $("curate-dialog").classList.remove("is-done");
  $("curate-pdf").value = "";
  $("curate-attest").checked = false;
  $("curate-job").hidden = true;
  $("curate-job").innerHTML = "";
  pdfName();
}
// A curated graph's title, short enough for its card: the one the model gives when it calls done, else the source's
// title when it was found, else the PDF's name; cut at a word before 60 characters.
function graphTitle(given, doc, filename) {
  const src = doc.nodes.find((n) => n.label === "Source")?.props || {};
  const slug = (doc.graph?.graph_id || "").split("/").pop();
  const pick = [given, src.title !== slug ? src.title : "", filename.replace(/\.pdf$/i, "")].find((t) => t && String(t).trim()) || "Curated source";
  const t = String(pick).replace(/\s+/g, " ").trim();
  if (t.length <= 60) return t;
  const cut = t.slice(0, 60).replace(/\s+\S*$/, "").replace(/[\s,;:–—-]+$/, "");
  return `${cut}…`;
}
async function curateOwnKey() {
  const job = $("curate-job");
  let st = null;
  try {
    const f = $("curate-pdf").files[0];
    if (!f) throw new Error("give the source's PDF");
    if (!$("curate-attest").checked) throw new Error("tick the statement that you have lawful access to this PDF");
    curating = true; finishNow = false; syncCurate();
    st = curateProgress(job);
    st.go("read", `Reading ${f.name}`);
    const src = await pdfText(f);
    if (src.chars < 200) throw new Error("the PDF has no text layer to read (a scan?); curating it needs text, which OCR would give");
    st.pages = src.pages;
    st.go("prepare", `${src.pages} pages of text read. Preparing SCALE in your browser (the first time, up to a minute)`);
    const stem = f.name.replace(/\.pdf$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "source";
    const s = await rpc({ op: "curate_start", text: src.text, name: `${stem}.txt`, version: commonsVersion });
    $("curate-schema").textContent = `Curated under SCALE Catalysis, schema scale-catal v${s.started.schema_version}, in this browser.`;
    st.go("curate", `Read 0 of ${st.pages} pages · waiting for the model`);
    const r = await modelLoop({ system: s.system, tools: s.tools, first: s.first, stop: "done",
      run: async (tool, args) => {
        const out = await rpc({ op: "curate_call", tool, args });
        st.tool(tool, args, out);
        return out;
      },
      wrapUp: FINISH_UP, stopNow: () => finishNow, progress: () => {}, alive: () => true });
    st.go("check", "Final check");
    const check = JSON.parse(await rpc({ op: "curate_call", tool: "check", args: {} }));
    const doc = await rpc({ op: "curate_graph" });
    curating = false;
    clearInterval(st.timer);
    const title = graphTitle(r.stopInput?.title, doc, f.name);
    doc.graph = { ...(doc.graph || {}), title };
    await openOwn({ id: `curated-${stem}`, title, from: "own-key" }, JSON.stringify(doc), null, doc);
    curateDone({ doc, check, r, pages: st.pages, secs: (Date.now() - st.t0) / 1000, title, log: st.log });
  } catch (e) {
    curating = false;
    if (st) { st.stage = "failed"; st.end("Failed", `<div class="gx-err">${esc(e.message || String(e))}</div>`); }
    else { job.hidden = false; job.innerHTML = `<div class="gx-err">${esc(e.message || String(e))}</div>`; }
  }
  syncCurate();
}
// The commons holds the source already, or an open contribution adds it: nothing was curated and nothing spent.
function showKnown(k) {
  const commons = manifest.find((e) => e.id === "catal-commons");
  $("curate-job").innerHTML = (k.in_commons
    ? `<div class="gx-state">Already in catal-commons</div><div>${k.in_commons.map((h) => `<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.file)}</a>`).join(", ")}</div>`
    : k.proposed ? `<div class="gx-state">Already proposed</div><div><a href="${esc(k.proposed.url)}" target="_blank" rel="noopener">#${esc(k.proposed.number)} ${esc(k.proposed.title)}</a>, waiting for review.</div>`
    : `<div class="gx-state">Already curated for you</div><div>You sent this source before (the same ${esc((k.same_as.by || []).map((b) => ({ text_sha256: "PDF", doi: "DOI", title_year: "title and year" })[b] || b.replace("identifier:", "")).join(", "))}); its graph is in your inbox.</div>`)
    + `<div class="gx-subtle">Nothing was curated, so nothing was spent.</div><p>`
    + (commons && k.in_commons ? `<button type="button" class="gx-btn gx-btn-tiny gx-btn-primary" id="known-open">Open catal-commons</button> ` : "")
    + (k.same_as ? `<button type="button" class="gx-btn gx-btn-tiny gx-btn-primary" id="known-mine">Open it</button> ` : "")
    + `<button type="button" class="gx-btn gx-btn-tiny" id="known-again">Curate it anyway</button></p>`;
  $("known-open")?.addEventListener("click", () => { $("curate-dialog").close(); location.hash = "g=catal-commons"; });
  $("known-mine")?.addEventListener("click", () => followJob(k.same_as.job, true));
  $("known-again").onclick = () => startCuration(true);
}
// A job under way is kept in this tab (its id), so that following it survives a reload or a sign-in; a reply lost on
// the way is asked for again, and the service forgets the result only when the page says it has it. An inbox job
// (inbox) waits its turn on the service's computer and stays readable in the reader's inbox, so it is followed more
// slowly and never forgotten from here.
const JOB = "scale-job";
async function followJob(id, inbox = false) {
  const job = $("curate-job");
  store.set(JOB, JSON.stringify({ id, inbox }));
  for (let misses = 0; ;) {
    let j;
    try { j = await call("GET", `/jobs/${id}`); misses = 0; } catch (e) {
      const lost = e.status === 404 || e.status === 401;
      if (e.status === 404) store.set(JOB, "");
      if (lost || ++misses > 10) {
        job.innerHTML = `<span class="gx-err">${esc(e.message)}${e.status === 401 ? "; sign in again to see the job" : ""}</span>`;
        return;
      }
      job.innerHTML = `<div class="gx-subtle">The service did not answer (${esc(e.message)}); asking again…</div>`;
      await new Promise((r) => setTimeout(r, Math.min(3000 * misses, 30000)));
      continue;
    }
    const log = (j.log || []).slice(-12).map((l) => `<li>${esc(l.t)} s · ${esc(l.msg)}</li>`).join("");
    const src = j.source ? `<div class="gx-subtle">${esc(j.source.pages)} pages · rights: ${esc(j.source.rights_class)} · schema ${esc(j.source.schema_version)}</div>` : "";
    const wait = j.position ? `<div class="gx-subtle">${esc(j.position)} ${j.position > 1 ? "sources wait" : "source waits"} before yours</div>` : "";
    job.innerHTML = `<div class="gx-state">${esc(j.state)}</div>${src}${wait}<ol>${log}</ol>`;
    if (j.known) { store.set(JOB, ""); showKnown(j.known); return; }
    if (j.state === "done") {
      job.innerHTML += `<div>${esc(j.summary || "")}</div>`
        + (j.withheld ? `<div class="gx-subtle">${esc(j.withheld)} quotes withheld for the source's rights (their sha256 kept).</div>` : "")
        + `<div class="gx-subtle">The graph is open on the canvas and private to you. Its card on the left downloads it or contributes it to catal-commons.</div>`;
      await openCurated(j.graph, j.receipt);
      collected(id, inbox);
      return;
    }
    if (j.state === "failed") {
      job.innerHTML += `<div class="gx-err">${esc(j.error)}</div>`;
      collected(id, inbox);
      return;
    }
    await new Promise((r) => setTimeout(r, inbox ? 10000 : 3000));
  }
}
function collected(id, inbox) {
  store.set(JOB, "");
  if (!inbox) call("POST", `/jobs/${id}/done`).catch(() => { /* the service forgets it within the hour anyway */ });
}
// The reader's own graph (curated by the service just now, or opened from a file) is kept in this tab with the
// service's receipt, and the address names it like any other (#g=…): a sign-in, which leaves the page and comes back,
// or a reload opens it again, and it stays in the list when another graph is opened.
const OWN = "scale-own";
let own = null;                                      // { entry, text, receipt, doc }
async function openOwn(entry, text, receipt = null, doc = null) {
  doc ||= JSON.parse(text);
  if (!Array.isArray(doc.nodes) || !Array.isArray(doc.links)) throw new Error("no nodes and links");
  own = { entry, text, receipt, doc };
  store.set(OWN, "");                                 // a graph too large to keep must not leave an older one behind
  // false when it is too large for the tab's storage (a few MB), which a larger one is not even tried in
  own.kept = !!text && text.length < 4e6 && store.set(OWN, JSON.stringify({ entry, text, receipt }));
  if (new URLSearchParams(location.hash.slice(1)).get("g") !== entry.id) history.pushState(null, "", `#g=${encodeURIComponent(entry.id)}`);
  await showOwn();
}
async function showOwn() {
  if (own.prUrl && /\/upload\//.test(own.prUrl)) { own.contributed = null; own.prUrl = ""; }   // by hand: GitHub alone knows
  // a graph curated before titles were concise carries its file's slug: give it its source's title
  if (own.entry.from === "own-key" && !/\s/.test(own.entry.title)) own.entry.title = graphTitle("", own.doc || JSON.parse(own.text), own.entry.title);
  await openGraph(own.entry, own.text, own.doc);
  G.receipt = own.receipt;
  renderGraphs();
  if (!own.kept && !own.entry.windowed) status("This graph is too large to keep in this tab: download it before you sign in or reload.");
  if (token && store.take(CONTRIB_PENDING) === own.entry.id) contribute();   // back from the sign-in a click asked for
}
// A graph the service just curated, with its receipt, which alone lets it be contributed (only that graph, unchanged,
// by the user it was curated for).
async function openCurated(doc, receipt) {
  if (!doc) return;
  const title = doc.graph?.title || "Curated source";
  const entry = { id: `curated-${title.toLowerCase().replace(/\W+/g, "-").slice(0, 40)}`, title, from: "service" };
  await openOwn(entry, JSON.stringify(doc), receipt || null);
}
// Contributing: one click. Signed out, the click signs the reader in with GitHub (their login is their credit) and the
// contribution goes on by itself when they come back; signed in, the service is asked whether catal-commons holds the
// source already (or an open pull request proposes it) and, if not, the pull request opens at once, for a person to
// review. A graph the service curated carries its receipt; one curated in this browser says so (curated_in_browser).
const CONTRIB_PENDING = "scale-contrib-pending";
let contribBusy = false;
// Contributed: a pull request to catal-commons exists (opened by the page or the service). The by-hand route says
// nothing about it, since only GitHub knows whether the reader uploaded the file.
const sentToCommons = () => !!(own?.contributed && own.prUrl && !/\/upload\//.test(own.prUrl));
function contribButton() {                       // on a personal graph's title line, before its label
  if (!own || (own.entry.from !== "own-key" && !own.receipt && !own.contributed)) return "";
  if (sentToCommons()) return "";                // its label says catal-commons now, linking the pull request
  if (!SERVICE) return `<button type="button" class="gx-graph-contrib" id="contrib-go"${contribBusy ? ' aria-disabled="true"' : ""}
    title="${GH_LOGIN ? "Sign in with GitHub, and the pull request to catal-commons opens with your graph attached, under your login (your credit)" : "GitHub's upload page opens and the graph downloads: drop it there, then Propose changes (your login is your credit)"}">${contribBusy ? "Contributing…" : "Contribute"}</button>`;
  return `<button type="button" class="gx-graph-contrib" id="contrib-go"${contribBusy ? ' aria-disabled="true"' : ""}
    title="${token ? "Contribute it to catal-commons: a pull request under your GitHub login, for a person to review" : "Contribute it to catal-commons: sign in with GitHub (your credit), and a pull request opens for a person to review"}">${contribBusy ? "Contributing…" : "Contribute"}</button>`;
}
const contribBody = (B) => ({ graph: B.doc, ...(B.receipt ? { receipt: B.receipt } : { curated_in_browser: true }) });
function wireContrib() {
  const b = $("contrib-go");
  if (!b) return;
  b.onclick = (ev) => {
    ev.stopPropagation();
    if (own?.prUrl) { window.open(own.prUrl, "_blank", "noopener"); return; }
    if (b.getAttribute("aria-disabled") !== "true" && !own?.contributed) contribute();
  };
}
function contributed(html, url = "") {           // the outcome, the card's Visibility, kept through a reload or sign-in
  if (own) {
    own.contributed = html; own.prUrl = url;
    store.set(OWN, JSON.stringify({ entry: own.entry, text: own.text, receipt: own.receipt, contributed: html, prUrl: url }));
  }
  contribBusy = false;
  renderGraphs();
}
// Without the service, a contribution is a pull request the reader opens on GitHub: the page downloads the graph with
// every quote withheld (the repository keeps no text of a source in copyright) and opens GitHub's upload page for
// graphs/catal-commons/incoming/; GitHub forks the repository and opens the pull request under the reader's login,
// their credit. The commons intake (.github/workflows/commons-intake.yml) folds it into catal-commons and opens the
// fold pull request for a person to review.
const REPO = window.SCALE_REPO || "trace-ai-org/scale";
async function contributeOnGitHub() {
  const B = G.base || G;
  const tab = window.open(`https://github.com/${REPO}/upload/main/graphs/catal-commons/incoming`, "_blank");   // in the click, so no
  if (tab) tab.opener = null;                                                                             // blocker stops it
  contribBusy = true; renderGraphs();
  try {
    const c = await rpc({ op: "contribution", text: JSON.stringify(B.doc) });
    const file = `${c.slug}.json`;
    download(file, new Blob([JSON.stringify(c.doc, null, 1)], { type: "application/json" }));
    contribBusy = false; renderGraphs();
    status(tab ? `On the GitHub tab: drop ${file} (just downloaded), then Propose changes and Create pull request.`
      : `Your browser blocked the GitHub tab: open github.com/${REPO}/upload/main/graphs/catal-commons/incoming and drop ${file} there.`);
  } catch (e) { contribBusy = false; renderGraphs(); status(`Contributing failed: ${e.message}`); }
}
// With a GitHub sign-in (service/github-login: a window opens on GitHub, and its token comes back to this page only,
// kept in memory for this one contribution and never saved), the page opens the pull request itself, the graph
// attached: it forks the repository (or brings the reader's fork up to date), adds the graph on a new branch of the
// fork, and opens GitHub's pull request page for it, the graph attached and the title and description filled in, for
// the reader to review and create under their login.
const GH_API = "https://api.github.com";
const GH_LOGIN = (window.SCALE_GITHUB_LOGIN || "").replace(/\/$/, "");
let ghToken = "";                                // this contribution's token, forgotten when it ends
function githubSignIn() {                        // called in the click itself, so that no blocker stops the window
  return new Promise((resolve, reject) => {
    const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
    const w = window.open(`${GH_LOGIN}/login?origin=${encodeURIComponent(location.origin)}&nonce=${nonce}`, "scale-github", "width=640,height=760");
    if (!w) { reject(new Error("your browser blocked the GitHub sign-in window: allow pop-ups for this page")); return; }
    const from = new URL(GH_LOGIN).origin;
    const end = (fn, v) => { window.removeEventListener("message", on); clearInterval(watch); fn(v); };
    const on = (e) => {
      if (e.origin !== from || e.data?.type !== "scale-github" || e.data.nonce !== nonce) return;
      if (e.data.token) end(resolve, { token: e.data.token, win: w });
      else { w.close(); end(reject, new Error(e.data.error || "GitHub did not sign you in")); }
    };
    window.addEventListener("message", on);
    const watch = setInterval(() => { if (w.closed) end(reject, new Error("the GitHub sign-in window was closed")); }, 800);
  });
}
async function gh(method, path, body) {
  const r = await fetch(GH_API + path, { method, body: body ? JSON.stringify(body) : undefined, headers: {
    Authorization: `Bearer ${ghToken}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
    ...(body ? { "Content-Type": "application/json" } : {}) } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.message || `GitHub answered ${r.status}`), { status: r.status });
  return j;
}
const base64 = (text) => {
  const bytes = new TextEncoder().encode(text);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
};
async function contributeWithToken(win = null) {   // win: the sign-in window, which then shows GitHub's pull request page
  const B = G.base || G;
  contribBusy = true; renderGraphs();
  const step = (t) => status(`Contributing to catal-commons: ${t}…`);
  try {
    step("preparing the graph");
    const c = await rpc({ op: "contribution", text: JSON.stringify(B.doc) });
    const title = B.doc.graph?.title || c.slug;
    step("checking your GitHub token");
    const me = await gh("GET", "/user");
    step("forking the repository");
    const fork = await gh("POST", `/repos/${REPO}/forks`, { default_branch_only: true });
    for (let i = 0; ; i++) {                       // a new fork takes a few seconds to be ready
      try { await gh("GET", `/repos/${fork.full_name}/git/ref/heads/main`); break; } catch (e) {
        if (i >= 30) throw new Error("GitHub is still making your fork; press Contribute again in a minute");
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    try { await gh("POST", `/repos/${fork.full_name}/merge-upstream`, { branch: "main" }); } catch { /* up to date, or it cannot: the branch still works */ }
    const head = await gh("GET", `/repos/${fork.full_name}/git/ref/heads/main`);
    const branch = `scale-contribute-${c.slug}-${Date.now().toString(36)}`.slice(0, 90);
    await gh("POST", `/repos/${fork.full_name}/git/refs`, { ref: `refs/heads/${branch}`, sha: head.object.sha });
    step("adding your graph");
    await gh("PUT", `/repos/${fork.full_name}/contents/graphs/catal-commons/incoming/${encodeURIComponent(c.slug)}.json`,
      { message: `catal-commons: contribute ${title}`, content: base64(JSON.stringify(c.doc, null, 1)), branch });
    // GitHub's "Open a pull request" page, the graph attached as its diff and the title and description filled in: the
    // reader reviews it and clicks Create pull request; the page watches for that pull request and marks the graph
    const evidenced = [...c.doc.nodes, ...c.doc.links].filter((x) => (x.props?.evidence || []).length).length;
    const prTitle = `catal-commons: contribute ${title}`;
    const prBody = [
      `Contributes **${title}** to catal-commons: \`graphs/catal-commons/incoming/${c.slug}.json\`, ${c.doc.nodes.length} records and ${c.doc.links.length} links, ${evidenced} with evidence.`,
      "", "Curated in the SCALE graph explorer by the curate-corpus skill, `scale source check` run in the browser against the source's text. "
        + `Every quote is withheld (${c.withheld}, kept as sha256 and length): the repository keeps no text of a source in copyright.`,
      "", "The commons intake folds it into catal-commons and opens the fold pull request crediting the contributor, for a person to review."].join("\n");
    const compare = `https://github.com/${REPO}/compare/main...${encodeURIComponent(me.login)}:${encodeURIComponent(branch)}?expand=1`
      + `&title=${encodeURIComponent(prTitle)}&body=${encodeURIComponent(prBody)}`;
    ghToken = "";
    if (win && !win.closed) win.location = compare; else window.open(compare, "_blank");
    status("On GitHub: review your pull request, then Create pull request.");
    contribBusy = false; renderGraphs();
    watchForPullRequest(`${me.login}:${branch}`, compare);
  } catch (e) {
    if (win && !win.closed) win.close();
    ghToken = ""; contribBusy = false; renderGraphs();
    status(`Contributing failed: ${e.message}`);
  }
}
// The pull request the reader creates on GitHub's page, looked for every few seconds for half an hour (GitHub's public
// API, no token): once it exists, the graph's label reads catal-commons and links it.
async function watchForPullRequest(head, compare) {
  const entry = own?.entry;
  for (let i = 0; i < 360 && own?.entry === entry && !sentToCommons(); i++) {
    await new Promise((r) => setTimeout(r, 5000));
    try {
      const r = await fetch(`${GH_API}/repos/${REPO}/pulls?state=all&head=${encodeURIComponent(head)}`, { headers: { Accept: "application/vnd.github+json" } });
      const prs = r.ok ? await r.json() : [];
      if (prs.length && own?.entry === entry) {
        contributed(`Sent to catal-commons: <a href="${esc(prs[0].html_url)}" target="_blank" rel="noopener">pull request #${esc(prs[0].number)}</a>`, prs[0].html_url);
        status(ready());
        return;
      }
    } catch { /* offline a moment: look again */ }
  }
}
async function contributeSignedIn() {
  const signIn = githubSignIn();                 // the window opens now, in the click
  contribBusy = true; renderGraphs(); status("Contributing to catal-commons: signing in with GitHub…");
  let win;
  try { ({ token: ghToken, win } = await signIn); } catch (e) { contribBusy = false; renderGraphs(); status(`Contributing stopped: ${e.message}.`); return; }
  await contributeWithToken(win);
}
async function contribute() {
  const B = G.base || G;
  if (contribBusy) return;
  const closed = graphLevels();                   // catal-commons is public: a graph with closed records never goes there
  if (closed.length) { status(`Not contributed: this graph holds records whose access is ${closed.join(", ")}; only a public graph goes to catal-commons, a public repository.`); return; }
  if (!SERVICE) return GH_LOGIN ? contributeSignedIn() : contributeOnGitHub();
  if (!token) { store.set(CONTRIB_PENDING, B.entry.id); signIn("contribute"); return; }
  contribBusy = true; renderGraphs();
  status("Contributing to catal-commons…");
  try {
    const r = await call("POST", "/contribute/check", contribBody(B));
    if (r.already) { status(ready()); return contributed(`In catal-commons already: <a href="${esc(r.already[0].url)}" target="_blank" rel="noopener">${esc(r.already[0].source)}</a>`, r.already[0].url); }
    if (r.proposed) { status(ready()); return contributed(`Proposed already: <a href="${esc(r.proposed.url)}" target="_blank" rel="noopener">pull request #${esc(r.proposed.number)}</a>`, r.proposed.url); }
    const o = await call("POST", "/contribute", contribBody(B));
    B.receipt = null;
    if (own) own.receipt = null;
    status(ready());
    contributed(o.pull_request ? `Sent to catal-commons: <a href="${esc(o.pull_request.url)}" target="_blank" rel="noopener">pull request #${esc(o.pull_request.number)}</a>, for review`
      : "In catal-commons already, or proposed", o.pull_request?.url || "");
  } catch (e) { contribBusy = false; renderGraphs(); status(`Contributing failed: ${e.message}`); }
}

async function start() {
  token = store.get("scale-token");
  try { own = JSON.parse(store.get(OWN) || "null"); } catch { own = null; }
  if (own) own.kept = true;
  readSignIn();
  renderAccount();
  try {
    const m = await (await fetch("data/graphs.json")).json();
    manifest = m.graphs || []; commonsVersion = m.commons_schema_version || "";
  } catch { manifest = []; }
  renderGraphs();
  window.onhashchange = () => openFromHash();
  $("graph-file").onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = "";                            // the same file can be opened again
    if (!f) return;
    const base = f.name.replace(/\.(json|zip)$/i, "");
    try {
      if (/\.zip$/i.test(f.name)) {                 // a knowledge-graph bundle (scale graph export --to kg-bundle)
        status("Reading the bundle…");
        const b = await readBundle(f, status);
        const id = `bundle-${base.replace(/\W+/g, "-")}`, title = b.version ? `${b.title} (v${b.version})` : b.title;
        if (b.view) { bundle = { b, id, title, width: DRAW_DEFAULT, centre: null, from: 0 }; await drawWindow({ centre: null, from: 0 }); }
        else await openOwn({ id, title, from: "" }, JSON.stringify(b.doc), null, b.doc);
      } else await openOwn({ id: `file-${base.replace(/\W+/g, "-")}`, title: base, from: "" }, await f.text());
    } catch (err) { status(ready()); alert(`Not a graph or a graph bundle: ${err.message}`); }
  };
  let pending = null;                               // a job followed before a reload or a sign-in: follow it again
  try { pending = JSON.parse(store.get(JOB) || "null"); } catch { /* none */ }
  if (pending?.id && token && SERVICE) { openCurate(); $("curate-job").hidden = false; followJob(pending.id, !!pending.inbox); }
  await openFromHash();
}

function renderGraphs() {
  const list = own ? [own.entry, ...manifest] : manifest;
  $("graphs").innerHTML = list.map((e) => {
    const [scope, c] = scopeOf(e), active = G && G.entry === e, open = active && !folded;
    const mineRow = own && e === own.entry, sent = mineRow && sentToCommons();
    const action = mineRow ? (sent ? `<a class="gx-graph-scope gx-graph-sent" href="${esc(own.prUrl)}" target="_blank" rel="noopener"
        title="Contributed to catal-commons: its pull request, for a person to review">catal-commons</a>` : contribButton() + `<span class="gx-graph-scope">${scope}</span>`) : "";
    return `<div role="listitem" class="gx-graph-row${action ? " has-action" : ""}" style="--dot:${c}"><button class="gx-graph-item${active ? " is-active" : ""}" type="button" data-id="${esc(e.id)}" title="${esc(e.title)}"
        aria-expanded="${open}"><span class="gx-graph-caret">${CARET}</span><span class="gx-graph-dot"></span>
        <span class="gx-graph-title">${esc(e.title)}</span>${action ? "" : `<span class="gx-graph-scope">${scope}</span>`}</button>`
      + (action ? `<span class="gx-graph-actions">${action}</span>` : "")
      + `${open ? details() : ""}</div>`;
  }).join("") || `<p class="gx-muted">No graphs are listed: open a file.</p>`;
  // the title gives way to the actions laid over its row's right end, by their measured width
  $("graphs").querySelectorAll(".gx-graph-row.has-action").forEach((r) => {
    r.querySelector(".gx-graph-item").style.paddingRight = `${r.querySelector(".gx-graph-actions").offsetWidth + 17}px`;
  });
  wireContrib();
  const gs = $("graph-show");
  if (gs) gs.onclick = (ev) => { ev.preventDefault(); showGraph(); };
  wireWindow();
  const st = $("schema-toggle");
  if (st) st.onclick = (ev) => { ev.preventDefault(); G.base ? leaveSchema() : showSchema(); };
  $("graphs").querySelectorAll(".gx-graph-item").forEach((b) => (b.onclick = (ev) => {
    if (b.classList.contains("is-active")) {      // the open graph: its row folds and unfolds its facts (in the schema
      if (G.base && !ev.target.closest(".gx-graph-caret")) { leaveSchema(); return; }   // view, only its caret does)
      folded = !folded;
      renderGraphs();
      $("graphs").querySelector(".gx-graph-item.is-active")?.focus();
      return;
    }
    const e = (own ? [own.entry, ...manifest] : manifest).find((x) => x.id === b.dataset.id);
    if (e) location.hash = `g=${encodeURIComponent(e.id)}`;
  }));
}
function details() {
  const B = G.base || G;                          // in the schema view, the facts are still the graph's
  const g = B.doc.graph || {}, e = B.entry, [scope] = scopeOf(e), mine = e.from === "service" || e.from === "own-key";
  const facts = [
    ["Type", `<a href="#" id="graph-show" title="Show the graph on the canvas">Knowledge graph</a>${G.base ? "" : ` <span class="gx-subtle">(shown)</span>`}${scope === "synthetic" ? ` <span class="gx-warn">synthetic example</span>` : ""}`],
    ["Size", `${B.doc.nodes.length.toLocaleString()} nodes · ${B.doc.links.length.toLocaleString()} rels · ${B.props.toLocaleString()} props`],
    g.version ? ["Release", esc(g.version)] : null,
    ["Visibility", mine ? "Private" : esc(((g.stewardship || {}).access || "public").replace(/^./, (c) => c.toUpperCase()))],
    g.created || g.date ? ["Created", esc(g.created || g.date)] : null,
    e.windowed ? null : ["Checksum", B.sha ? `<span class="gx-sha" title="sha256 ${esc(B.sha)}">${esc(B.sha.slice(0, 12))}…</span>` : "…"],
    ...windowRows(e),
    ["Source", e.from === "service" ? "curated by the service just now" : e.from === "own-key" ? "curated by you, in this browser" : e.from ? `<a href="https://github.com/trace-ai-org/scale/tree/main/${esc(e.from)}">${esc(e.from)}</a>` : "a file on this computer"],
    ["Schema", `<a href="#" id="schema-toggle" title="${G.base ? "Back to the graph" : "Show the schema on the canvas"}">${esc(g.schema || "scale-catal")} v${esc(g.schema_version || "?")}</a>${G.base ? ` <span class="gx-subtle">(shown)</span>` : ""}<br>
      ${B.labels.size} labels · ${B.rels.size} rel types · ${schemaPropCount(B).toLocaleString()} props`],
  ].filter(Boolean);
  return `<div class="gx-graph-detail"><dl class="gx-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
}

// A bundle with more records than the explorer draws at once is drawn a window at a time (bundle.js): its sources in
// an order (by year, the oldest first; or around a source, those most like it first) and as many records as the
// reader sets (the width). The arrows step from one window to the next, so that the windows cover the whole graph; a
// double-click on a record, or a search match not drawn, draws around its source; a year jumps in the order by year.
let bundle = null;                                 // { b, id, title, width, centre, from, drawn }
async function drawWindow({ centre = bundle.centre, from = 0, focus = null } = {}) {
  const { b } = bundle;
  status(centre == null ? `Drawing records from ${from.toLocaleString()} on…` : `Drawing around “${trunc(b.sources[centre].title, 60)}”…`);
  await new Promise((r) => setTimeout(r, 30));       // the status shows before the work
  const w = b.view(centre, from, bundle.width);
  Object.assign(bundle, { centre, from: w.from, drawn: w });
  await openOwn({ id: bundle.id, title: bundle.title, from: "", windowed: true }, null, null, w.doc);
  status(`Records ${w.from.toLocaleString()}–${w.to.toLocaleString()} of ${w.total.toLocaleString()} drawn. Search covers them all.`);
  if (focus && G.byId.has(focus)) goTo(focus);
}
function windowRows(e) {
  if (!e.windowed || bundle?.id !== e.id || !bundle.drawn) return [];
  const { b, drawn: w, width } = bundle, span = w.years ? ` · ${w.years[0] === w.years[1] ? w.years[0] : `${w.years[0]}–${w.years[1]}`}` : "";
  return [
    ["Order", w.order === "year" ? "Sources by year, the oldest first"
      : `Around <a href="#" id="window-centre" title="${esc(w.title)}">${esc(trunc(w.title, 44))}</a>${w.year ? ` (${w.year})` : ""}, the sources most
        like it first · <a href="#" id="window-by-year">by year</a>`],
    ["Window", `<div class="gx-window"><button type="button" class="gx-btn gx-btn-tiny" id="window-prev" title="The window before"${w.from ? "" : " disabled"}>◀</button>
      <span>records ${w.from.toLocaleString()}–${w.to.toLocaleString()} of ${w.total.toLocaleString()}${span}</span>
      <button type="button" class="gx-btn gx-btn-tiny" id="window-next" title="The window after"${w.to < w.total ? "" : " disabled"}>▶</button></div>
      <input type="range" class="gx-range" id="window-from" min="0" max="${w.total - 1}" step="1000" value="${w.from}" aria-label="Where the window starts">
      <span class="gx-subtle">${w.sources.toLocaleString()} sources; ${w.records.toLocaleString()} records drawn with what they link to</span>`],
    ["Width", `<input type="range" class="gx-range" id="window-width" min="5000" max="80000" step="5000" value="${width}"
      aria-label="Records drawn at once"><span id="window-width-n">${width.toLocaleString()}</span> records at once`],
    ["Go to", `<select class="gx-select" id="window-year" aria-label="Where a year's sources begin, in the order by year"><option value="">a year…</option>${
      b.years.map((y) => `<option value="${y}">${y}</option>`).join("")}</select>`],
  ];
}
function wireWindow() {
  if (!$("window-from") || !bundle?.drawn) return;
  const w = bundle.drawn, go = (from) => drawWindow({ from });
  $("window-prev").onclick = () => go(Math.max(0, w.from - bundle.width));
  $("window-next").onclick = () => go(w.to);
  const f = $("window-from"), r = $("window-width"), y = $("window-year");
  f.onchange = () => go(Number(f.value));
  r.oninput = () => { $("window-width-n").textContent = Number(r.value).toLocaleString(); };
  r.onchange = () => { bundle.width = Number(r.value); go(w.from); };
  y.onchange = () => { if (y.value) drawWindow({ centre: null, from: bundle.b.startOf(Number(y.value)) }); };
  $("window-centre")?.addEventListener("click", (ev) => { ev.preventDefault(); goTo(w.source); });
  $("window-by-year")?.addEventListener("click", (ev) => { ev.preventDefault(); drawWindow({ centre: null, from: 0 }); });
}
// The bundle is searched whole: a record not drawn is drawn around its own source when chosen.
async function searchBundle(q) {
  leaveSchema();
  card("Matching records", `<p class="gx-meta">Searching all ${bundle.b.records.toLocaleString()} records…</p>`);
  await new Promise((r) => setTimeout(r, 30));
  const t0 = performance.now(), hits = bundle.b.search(q, 50), ms = Math.round(performance.now() - t0);
  S.match = new Set(hits.map((h) => h.record).filter((id) => G.byId.has(id)));
  const missing = [...S.match].filter((id) => !S.nodes.some((n) => n.id === id));
  if (missing.length) { missing.forEach((id) => S.extra.add(id)); draw(); } else markMatches();
  const shown = S.nodes.filter((n) => S.match.has(n.id));
  if (shown.length) setTimeout(() => { S.viewMoved = true; fit(false, shown); }, 300);
  card(`Matching records · ${hits.length}${hits.length === 50 ? "+" : ""}`, (hits.length ? hits.map((h) => `<div class="gx-hit" data-id="${esc(h.record)}" data-source="${h.source ?? ""}">
      <div class="gx-quote">“${esc(trunc(h.text, 260))}”</div>
      <div class="gx-meta">${esc(h.label)}: ${esc(trunc(h.name, 60))} · ${esc(trunc(h.sourceTitle, 80))}${h.year ? ` (${h.year})` : ""}${
        G.byId.has(h.record) ? "" : " · <i>not drawn yet: a click draws around it</i>"}</div></div>`).join("")
    : `<p>Nothing in the ${bundle.b.records.toLocaleString()} records matches “${esc(q)}”.</p>`)
    + `<p class="gx-meta">Searched every record of the bundle (${ms.toLocaleString()} ms).</p>`);
  $("card-body").querySelectorAll(".gx-hit").forEach((el) => (el.onclick = () => {
    const id = el.dataset.id, s = el.dataset.source;
    if (G.byId.has(id)) goTo(id); else if (s !== "") drawWindow({ centre: Number(s), from: 0, focus: id });
  }));
}

// ------------------------------------------------------------------------------------------ a large graph: a canvas
// A graph of more than CANVAS_GRAPH records is drawn on a canvas, not as SVG elements, and laid out off the page's
// thread (layout.js): the page stays responsive while the layout settles, and moving or zooming redraws at once.
const layout = { worker: null, run: 0, running: false };
const HINT = document.querySelector(".gx-hint").textContent;
function hint() {
  document.querySelector(".gx-hint").textContent = !S.big ? HINT : G?.entry.windowed
    ? "A window of the bundle: its card's arrows step through the whole graph · click a record for its details · double-click one to draw around its source · search covers every record"
    : "A large graph, drawn on a canvas · scroll to zoom · click a record for its details · double-click one to expand its neighbourhood";
}
function drawCanvas() {
  S.sim?.stop();
  S.big = true;
  S.zoom.scaleExtent([0.005, 4]);                     // a large graph is seen whole from further away
  d3.select("#nodes").selectAll("*").remove(); d3.select("#links").selectAll("*").remove();
  $("gx-canvas").hidden = false;
  S.at = new Map(S.nodes.map((d) => [d.id, d]));
  S.nodeGroups = new Map(); S.linkGroups = new Map();
  for (const d of S.nodes) { d.r = radius(d.id); const c = colourForKind(d.label); (S.nodeGroups.get(c) || S.nodeGroups.set(c, []).get(c)).push(d); }
  for (const e of S.links) {
    const a = S.at.get(e.source), b = S.at.get(e.target), c = colourForEdge(e.type);
    if (a && b) (S.linkGroups.get(c) || S.linkGroups.set(c, []).get(c)).push([a, b]);
  }
  S.quad = null;
  S.draw = () => { if (!S.frame) S.frame = requestAnimationFrame(paint); };
  S.sim = { alpha: () => (layout.running ? 0.5 : 0), alphaMin: () => 0.001, alphaTarget() { return this; }, stop: layoutStop, restart() { layoutRun(); return this; } };
  S.viewMoved = false;
  hint();
  if (S.layout !== "force") applyLayout(S.layout, false); else layoutRun();
}
function leaveCanvas() {
  layoutStop();
  S.big = false;
  S.zoom.scaleExtent([0.05, 4]);
  $("gx-canvas").hidden = true;
  hint();
}
// records new to the canvas start beside the others of their source (REPORTED_IN): each source's records in a disc,
// the sources on a spiral, the largest in the middle; the layout then has little left to do
function seed(W, H) {
  const fresh = S.nodes.filter((d) => !Number.isFinite(d.x));
  if (!fresh.length) return;
  const group = new Map();
  for (const e of G.doc.links) if (e.type === PROVENANCE_EDGE && !group.has(e.source)) group.set(e.source, e.target);
  const members = new Map();
  for (const d of fresh) { const g = group.get(d.id) ?? (d.label === PROVENANCE_KIND ? d.id : ""); (members.get(g) || members.set(g, []).get(g)).push(d); }
  const cell = 26;
  let filled = 0, k = 0;
  for (const list of [...members.values()].sort((a, b) => b.length - a.length)) {
    const r = cell * Math.sqrt(filled + list.length / 2), a = k++ * 2.399963, spread = (cell * Math.sqrt(list.length)) / 2;
    const cx = W / 2 + r * Math.cos(a), cy = H / 2 + r * Math.sin(a);
    for (const d of list) { const t = Math.random() * 2 * Math.PI, s = spread * Math.sqrt(Math.random()); d.x = cx + s * Math.cos(t); d.y = cy + s * Math.sin(t); }
    filled += list.length;
  }
}
function layoutRun() {
  const [W, H] = size(), n = S.nodes.length, at = new Map(S.nodes.map((d, i) => [d.id, i]));
  seed(W, H);
  const x = new Float32Array(n), y = new Float32Array(n), degree = new Uint16Array(n), links = new Int32Array(2 * S.links.length);
  S.nodes.forEach((d, i) => { x[i] = d.x; y[i] = d.y; degree[i] = Math.min(65535, S.degree.get(d.id) || 0); });
  S.links.forEach((e, k) => { links[2 * k] = at.get(e.source.id ?? e.source); links[2 * k + 1] = at.get(e.target.id ?? e.target); });
  if (!layout.worker) {
    layout.worker = new Worker("layout.js");
    layout.worker.onmessage = ({ data }) => {
      if (data.run !== layout.run || !S.big) return;
      const p = data.pos;
      S.nodes.forEach((d, i) => { d.x = p[2 * i]; d.y = p[2 * i + 1]; });
      S.quad = null;
      layout.running = !data.done;
      S.draw();
      if (!S.viewMoved) fit(!data.done);
    };
  }
  layout.running = true;
  layout.worker.postMessage({ op: "layout", run: ++layout.run, x, y, links, degree, alpha: 0.6, distance: 55, charge: -110, cx: W / 2, cy: H / 2 },
    [x.buffer, y.buffer, links.buffer, degree.buffer]);
}
function layoutStop() {
  layout.running = false;
  if (layout.worker) layout.worker.postMessage({ op: "stop", run: ++layout.run });
}
function paint() {
  S.frame = 0;
  if (!S.big) return;
  const c = $("gx-canvas"), [W, H] = size(), dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
  const ctx = c.getContext("2d"), t = d3.zoomTransform(svg.node()), m = S.match, css = getComputedStyle(document.documentElement);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.setTransform(dpr * t.k, 0, 0, dpr * t.k, dpr * t.x, dpr * t.y);
  ctx.lineWidth = 1 / t.k;
  ctx.globalAlpha = m ? 0.08 : 0.35;
  for (const [colour, list] of S.linkGroups) {
    ctx.strokeStyle = colour;
    ctx.beginPath();
    for (const [a, b] of list) { ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); }
    ctx.stroke();
  }
  const tiny = t.k * NODE_R_MIN < 1.5;              // far away, a record is a square of a pixel or two
  ctx.globalAlpha = m ? 0.25 : 0.9;
  for (const [colour, list] of S.nodeGroups) {
    ctx.fillStyle = colour;
    ctx.beginPath();
    for (const d of list) {
      if (tiny) ctx.rect(d.x - d.r, d.y - d.r, 2 * d.r, 2 * d.r);
      else { ctx.moveTo(d.x + d.r, d.y); ctx.arc(d.x, d.y, d.r, 0, 2 * Math.PI); }
    }
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  const ring = (d, w) => { ctx.beginPath(); ctx.arc(d.x, d.y, d.r + 3 / t.k, 0, 2 * Math.PI); ctx.lineWidth = w / t.k; ctx.stroke(); };
  ctx.strokeStyle = css.getPropertyValue("--select").trim() || "#ffae3a";
  if (m) for (const d of S.nodes) if (m.has(d.id)) { ctx.fillStyle = colourForKind(d.label); ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, 2 * Math.PI); ctx.fill(); ring(d, 1.5); }
  const sel = S.selected && S.at.get(S.selected);
  if (sel) ring(sel, 3);
  snapshot(c, t);
  if (t.k >= FAR_ZOOM) {                             // near enough, the names of the records in view
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = getComputedStyle(document.body).color;
    let shown = 0;
    for (const d of S.nodes) {
      const sx = t.x + d.x * t.k, sy = t.y + d.y * t.k;
      if (sx < 0 || sy < 0 || sx > W || sy > H) continue;
      ctx.fillText(trunc(d.name, 22), sx + d.r * t.k + 3, sy + 4);
      if (++shown >= 600) break;
    }
  }
}
// While the view moves, a large graph's last picture moves with it, scaled and shifted at once; the graph is drawn anew
// when the view stops. (The names, drawn on top, come back with that new picture.)
const SNAP_GRAPH = 20000;
function snapshot(c, t) {
  if (S.nodes.length < SNAP_GRAPH) { S.snapAt = null; return; }
  S.snap ||= document.createElement("canvas");
  if (S.snap.width !== c.width || S.snap.height !== c.height) { S.snap.width = c.width; S.snap.height = c.height; }
  const ctx = S.snap.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, c.width, c.height); ctx.drawImage(c, 0, 0);
  S.snapAt = { k: t.k, x: t.x, y: t.y };
}
function moved() {
  if (!S.snapAt) { S.draw(); return; }
  if (!S.quick) S.quick = requestAnimationFrame(() => {
    S.quick = 0;
    const c = $("gx-canvas"), ctx = c.getContext("2d"), t = d3.zoomTransform(svg.node()), a = S.snapAt, k = t.k / a.k, dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, c.width, c.height);
    ctx.drawImage(S.snap, dpr * (t.x - a.x * k), dpr * (t.y - a.y * k), S.snap.width * k, S.snap.height * k);
  });
  clearTimeout(S.still);
  S.still = setTimeout(() => S.draw(), 160);
}
// the record under the pointer, if any
function hit(ev) {
  const t = d3.zoomTransform(svg.node()), [px, py] = t.invert(d3.pointer(ev, svg.node()));
  S.quad ||= d3.quadtree(S.nodes, (d) => d.x, (d) => d.y);
  const d = S.quad.find(px, py, NODE_R_MAX + 8 / t.k);
  return d && Math.hypot(d.x - px, d.y - py) <= d.r + 6 / t.k ? d.id : null;
}

// ------------------------------------------------------------------------------------------ the schema view
// The graph's schema as the graph uses it: a node per label, a link per label -> relationship type -> label, each with
// the properties its records carry and how many of them carry each. It is drawn on the canvas in place of the graph
// (G.base keeps the graph); search, questions and the tools still read the graph.
const SCHEMA = "schema:";
function schemaOf(B) {
  const nodes = new Map(), links = new Map();
  const tally = (into, props) => { for (const k of Object.keys(props || {})) into.set(k, (into.get(k) || 0) + 1); };
  for (const n of B.doc.nodes) {
    const s = nodes.get(n.label) || nodes.set(n.label, { n: 0, props: new Map() }).get(n.label);
    s.n += 1; tally(s.props, n.props);
  }
  for (const e of B.doc.links) {
    const a = B.byId.get(e.source)?.label ?? "?", b = B.byId.get(e.target)?.label ?? "?", key = `${a}|${e.type}|${b}`;
    const s = links.get(key) || links.set(key, { a, b, type: e.type, n: 0, props: new Map() }).get(key);
    s.n += 1; tally(s.props, e.props);
  }
  const props = (s, what) => Object.fromEntries([["records", `${s.n.toLocaleString()} ${what}`],
    ...[...s.props].sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
      .map(([k, c]) => [k === "name" || k === "records" ? `${k} (property)` : k, `on ${c.toLocaleString()} of ${s.n.toLocaleString()}`])]);
  return {
    graph: { title: `Schema of ${B.doc.graph?.title || B.entry.title}` },
    nodes: [...nodes].map(([l, s]) => ({ id: `${SCHEMA}label/${l}`, label: l, props: { name: l, ...props(s, s.n === 1 ? "record" : "records") } })),
    links: [...links.values()].map((s) => ({ id: `${SCHEMA}rel/${s.a}/${s.type}/${s.b}`, type: s.type, source: `${SCHEMA}label/${s.a}`,
      target: `${SCHEMA}label/${s.b}`, props: props(s, s.n === 1 ? "link" : "links") })),
  };
}
function schemaPropCount(B) {
  const seen = new Set();
  for (const n of B.doc.nodes) for (const k of Object.keys(n.props || {})) seen.add(`${n.label}.${k}`);
  for (const e of B.doc.links) for (const k of Object.keys(e.props || {})) seen.add(`${e.type}.${k}`);
  return seen.size;
}
function showSchema() {
  if (!G || G.base) return;
  const B = G, doc = schemaOf(B);
  const byId = new Map(doc.nodes.map((n) => [n.id, n])), out = new Map(), inn = new Map();
  for (const e of doc.links) {
    (out.get(e.source) || out.set(e.source, []).get(e.source)).push(e);
    (inn.get(e.target) || inn.set(e.target, []).get(e.target)).push(e);
  }
  B.view = { kind: S.kind, rel: S.rel, sources: S.sources, hidden: S.hidden, extra: S.extra, pos: S.pos };
  G = { ...B, doc, byId, out, inn, base: B };
  Object.assign(S, { kind: null, rel: null, sources: true, hidden: new Set(), extra: new Set(), match: null, selected: null, pos: new Map(), nodes: [] });
  $("card").hidden = true;
  renderGraphs(); renderLegend(); showLegend(); draw();
  status(`The schema of ${B.entry.title}: ${doc.nodes.length} labels, ${doc.links.length} label-to-label relationships. Click Knowledge graph or its name on the left to go back.`);
}
function leaveSchema() {
  if (!G || !G.base) return;
  const B = G.base;
  G = B;
  Object.assign(S, { ...B.view, match: null, selected: null, nodes: [] });
  $("card").hidden = true;
  renderGraphs(); renderLegend(); showLegend(); draw();
  status(ready());
}

// the graph itself on the canvas: back from the schema, else fitted to the view
function showGraph() {
  if (!G) return;
  if (G.base) leaveSchema(); else fit();
}

async function openFromHash() {
  const want = new URLSearchParams(location.hash.slice(1)).get("g");
  if (own && own.entry.id === want) {
    if (!(G && G.entry === own.entry)) await showOwn();
    return;
  }
  const entry = manifest.find((g) => g.id === want) || manifest[0];
  if (!entry || (G && G.entry === entry)) return;
  status(`Loading ${entry.title}…`);
  $("empty").hidden = false;
  $("empty").innerHTML = `<p>Loading graph…</p>`;
  const text = await (await fetch(entry.path)).text();
  await openGraph(entry, text);
}

async function openGraph(entry, text, doc = null) {
  doc ||= JSON.parse(text);
  if (!Array.isArray(doc.nodes) || !Array.isArray(doc.links)) throw new Error("no nodes and links");
  const byId = new Map(doc.nodes.map((n) => [n.id, n]));
  const out = new Map(), inn = new Map(), labels = new Map(), rels = new Map();
  let props = 0;
  for (const n of doc.nodes) { labels.set(n.label, (labels.get(n.label) || 0) + 1); props += Object.keys(n.props || {}).length; }
  for (const e of doc.links) {
    (out.get(e.source) || out.set(e.source, []).get(e.source)).push(e);
    (inn.get(e.target) || inn.set(e.target, []).get(e.target)).push(e);
    rels.set(e.type, (rels.get(e.type) || 0) + 1);
    props += Object.keys(e.props || {}).length;
  }
  kindColour.clear(); edgeColour.clear();
  [...labels].sort((a, b) => b[1] - a[1]).forEach(([l]) => colourForKind(l));
  [...rels].sort((a, b) => b[1] - a[1]).forEach(([t]) => colourForEdge(t));
  G = { entry, file: `${entry.id}.json`, doc, byId, out, inn, labels, rels, props, sha: "" };
  folded = false;
  Object.assign(S, { kind: null, rel: null, sources: false, hidden: new Set(), extra: new Set(), match: null, selected: null, pos: new Map(), nodes: [] });
  $("card").hidden = true;
  renderGraphs();
  renderLegend();
  showLegend();
  draw();
  const g = G;
  if (!text) return;                                // a bundle drawn around a source: the bundle searches itself
  sha256(text).then((h) => { if (G && G.entry === entry) { G.sha = h; renderGraphs(); } });
  await rpc({ op: "put", name: G.file, text }).then(() => { g.loaded = true; })
    .catch((err) => status(`SCALE could not load the graph: ${err.message}`));
  if (G !== g) return;
  status(ready());
  warm(g);
}
async function sha256(text) {
  try {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch { return ""; }
}
// The first search of a graph builds its evidence index (seconds for thousands of quotes); build it now, while the
// reader looks at the graph. The toolkit keeps it with the loaded graph.
async function warm(g) {
  if (!g.doc.nodes.some((n) => (n.props?.evidence || []).some((e) => e.quote))) return;
  status("Indexing the evidence…");
  try { await scale("search", { query: "catalyst", k: 1 }); } catch { /* reported when the reader searches */ }
  if (G === g) status(ready());
}

// ------------------------------------------------------------------------------------------ colours, names, values
function colourForKind(k) { if (!kindColour.has(k)) kindColour.set(k, KIND_PALETTE[kindColour.size % KIND_PALETTE.length]); return kindColour.get(k); }
// one arrowhead per edge colour, filled with it, so a link's head is the colour of its line (an attribute, not a style,
// so that an exported SVG keeps it)
d3.select("#gx-defs").selectAll("marker").data(EDGE_PALETTE).join("marker")
  .attr("id", (_, i) => `gx-arrow-${i}`).attr("viewBox", "0 -5 10 10").attr("refX", 9).attr("refY", 0)
  .attr("markerWidth", 10).attr("markerHeight", 10).attr("orient", "auto").attr("markerUnits", "userSpaceOnUse")
  .append("path").attr("d", "M0,-5 L10,0 L0,5").attr("fill", (c) => c).attr("fill-opacity", 0.8);
const arrowFor = (t) => `url(#gx-arrow-${EDGE_PALETTE.indexOf(colourForEdge(t))})`;
function colourForEdge(t) { if (!edgeColour.has(t)) edgeColour.set(t, EDGE_PALETTE[edgeColour.size % EDGE_PALETTE.length]); return edgeColour.get(t); }
const name = (n) => (n && (n.props?.name || n.props?.title || n.props?.formula || n.id.split("/").pop())) || "?";
const trunc = (s, n) => (String(s).length > n ? `${String(s).slice(0, n - 1)}…` : String(s));
function value(v) {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) return v.map(value).join("; ");
  if (typeof v !== "object") return esc(v);
  if ("status" in v && (v.status !== "present" || "value" in v || "text_value" in v || "lower" in v || "upper" in v || "boolean_value" in v)) {
    if (v.status !== "present") return `<span class="gx-subtle">${esc(v.status)}${v.missing_reason ? ` (${esc(v.missing_reason)})` : ""}</span>`;
    const num = "value" in v ? v.value : "lower" in v || "upper" in v ? `${v.lower ?? ""}–${v.upper ?? ""}` : v.text_value ?? v.boolean_value;
    const shown = `${esc(num)}${v.unit && v.unit !== "1" ? ` ${esc(v.unit)}` : ""}`;
    return v.as_reported && String(v.as_reported) !== String(num) ? `${shown} <span class="gx-subtle">(as reported: ${esc(v.as_reported)})</span>` : shown;
  }
  const parts = Object.entries(v);                  // a map of values (conditions, a feed's composition): one per name
  if (parts.length && parts.every(([, x]) => x && typeof x === "object" && "status" in x)) return parts.map(([k, x]) => `${esc(k.replace(/_/g, " "))}: ${value(x)}`).join("; ");
  return `<code>${esc(JSON.stringify(v))}</code>`;
}
const idLinks = (html) => html.replace(/((?:ex|scale|[a-z][a-z0-9-]*):[A-Za-z0-9_.\/#-]+)/g,
  (m, id) => (G && G.byId.has(id) ? `<a href="#" data-id="${esc(id)}">${esc(id)}</a>` : m));
const wireIds = (root) => root.querySelectorAll("a[data-id]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); goTo(a.dataset.id); }));

// ------------------------------------------------------------------------------------------ what is drawn
function visible() {
  const showNode = (n) => !S.hidden.has(n.id) && (S.extra.has(n.id) || ((S.sources || n.label !== PROVENANCE_KIND) && (!S.kind || n.label === S.kind)));
  const nodes = G.doc.nodes.filter(showNode);
  const ids = new Set(nodes.map((n) => n.id));
  let crossing = 0;
  const links = [];
  for (const e of G.doc.links) {
    const a = ids.has(e.source), b = ids.has(e.target);
    if (a !== b) crossing += 1;
    if (a && b && (!S.rel || e.type === S.rel)) links.push(e);
  }
  return { nodes, links, crossing };
}
// A node's radius depends on its own number of links, never on the rest of the graph, so a node and the arrowheads
// (a fixed size) keep their proportions however large the graph grows.
const radius = (id) => Math.min(NODE_R_MAX, NODE_R_MIN + NODE_R_STEP * Math.sqrt(Math.max((S.degree.get(id) || 0) - 1, 0)));
const linkLabelsShown = () => (S.linkLabels === "on" ? true : S.linkLabels === "off" ? false : S.links.length <= LABELS_AUTO_MAX);

const svg = d3.select("#svg"), zoomLayer = d3.select("#zoom-layer");
function size() { const r = $("canvas").getBoundingClientRect(); return [Math.max(r.width, 320), Math.max(r.height, 320)]; }

function draw() {
  for (const n of S.nodes) S.pos.set(n.id, n);                     // keep where nodes were across redraws
  const { nodes, links, crossing } = visible();
  S.nodes = nodes.map((n) => { const p = S.pos.get(n.id); return { id: n.id, label: n.label, name: name(n), ...(p ? { x: p.x, y: p.y, fx: p.pinned ? p.fx : null, fy: p.pinned ? p.fy : null, pinned: p.pinned } : {}) }; });
  S.links = links.map((e) => ({ id: e.id, type: e.type, source: e.source, target: e.target }));
  $("summary").textContent = `Displaying ${nodes.length} nodes, ${links.length} relationships.`
    + (S.kind && crossing ? ` ${crossing} more connect them to other labels.` : "");
  $("unhide-btn").hidden = !S.hidden.size;
  $("unhide-label").textContent = `Show hidden (${S.hidden.size})`;
  $("labels-btn").setAttribute("aria-pressed", String(linkLabelsShown()));
  if (!nodes.length) {
    $("empty").hidden = false;
    $("empty").innerHTML = `<p>Nothing to show: pick another label or relationship type.</p>`;
    d3.select("#nodes").selectAll("*").remove(); d3.select("#links").selectAll("*").remove();
    return;
  }
  $("empty").hidden = true;
  const [W, H] = size();
  svg.attr("viewBox", `0 0 ${W} ${H}`);
  if (!S.zoom) {
    S.zoom = d3.zoom().scaleExtent([0.05, 4]).on("zoom", (ev) => {
      zoomLayer.attr("transform", ev.transform);
      if (S.big) moved();
      if (ev.sourceEvent) S.viewMoved = true;
      syncFar(ev.transform.k);
    });
    svg.call(S.zoom).on("dblclick.zoom", null);
    svg.on("click.inspector", (ev) => {
      if (S.big) { const id = hit(ev); id ? select(id) : clearSelection(); return; }
      if (ev.target === svg.node()) clearSelection();
    });
    svg.on("dblclick.canvas", (ev) => { if (!S.big) return; const id = hit(ev); if (id) expand(id); });
  }
  // degree, for the radius and the repulsion
  S.degree = new Map();
  for (const e of S.links) { S.degree.set(e.source, (S.degree.get(e.source) || 0) + 1); S.degree.set(e.target, (S.degree.get(e.target) || 0) + 1); }
  const degs = [...S.degree.values()];
  S.maxDeg = degs.length ? Math.max(...degs) : 0;
  S.minDeg = degs.length ? Math.min(...degs) : 0;
  if (nodes.length > CANVAS_GRAPH) return drawCanvas();
  if (S.big) leaveCanvas();

  const showLabels = linkLabelsShown();
  const link = d3.select("#links").selectAll("g").data(S.links, (d) => d.id).join((enter) => {
    const g = enter.append("g");
    g.append("path");
    g.append("text").attr("class", "gx-link-label").attr("dy", -3);
    return g;
  });
  link.select("path").attr("stroke", (d) => colourForEdge(d.type)).attr("stroke-opacity", 0.55)
    .attr("marker-end", (d) => arrowFor(d.type));
  // links are drawn as arcs: each bends to its left, so a link and its reverse part, and the links between one pair
  // of nodes fan out (the k-th of n bends further)
  const pairs = new Map();
  for (const e of S.links) {
    const a = e.source.id ?? e.source, b = e.target.id ?? e.target, key = a < b ? `${a}|${b}` : `${b}|${a}`;
    const list = pairs.get(key) || []; list.push(e); pairs.set(key, list);
  }
  for (const list of pairs.values()) list.forEach((e, i) => { e.bend = 0.16 + 0.14 * i; });
  link.select("text").text((d) => (showLabels ? d.type : "")).attr("fill", (d) => colourForEdge(d.type));
  link.on("click", (ev, d) => { ev.stopPropagation(); showDetails(G.doc.links.find((e) => e.id === d.id)); });

  const node = d3.select("#nodes").selectAll("g").data(S.nodes, (d) => d.id).join((enter) => {
    const g = enter.append("g").attr("class", "gx-node");
    g.append("circle");
    g.append("text").attr("dy", "0.32em");
    return g;
  });
  node.call(drag());
  node.on("dblclick", (ev, d) => { ev.stopPropagation(); expand(d.id); });
  node.classed("selected", (d) => d.id === S.selected);
  node.select("circle").attr("r", (d) => radius(d.id)).attr("fill", (d) => colourForKind(d.label));
  node.select("text").text((d) => trunc(d.name, 22));
  markMatches();

  // the layout: as the graph grows, shorter links, weaker repulsion and less padding keep its clusters on screen
  const linkCount = S.links.length;
  let hubId = null, hubDeg = 0;
  for (const [id, d] of S.degree) if (d > hubDeg) { hubId = id; hubDeg = d; }
  const isStar = linkCount > 0 && hubDeg >= linkCount * 0.5;
  const isBig = S.nodes.length > BIG_GRAPH;
  const linkDistance = isStar ? 180 : isBig ? 55 : 130;
  const charge = isStar ? -520 : isBig ? -110 : -320;
  const pad = isBig ? 6 : 24;
  const lines = link.select("path").nodes(), labels = link.select("text").nodes(), groups = node.nodes();
  S.draw = () => tick(lines, labels, groups, showLabels);
  S.viewMoved = false;
  syncFar(d3.zoomTransform(svg.node()).k);
  if (S.sim) S.sim.stop();
  S.sim = d3.forceSimulation(S.nodes)
    .force("link", d3.forceLink(S.links).id((d) => d.id).distance(linkDistance).strength(isStar ? 0.55 : 0.7))
    .force("charge", d3.forceManyBody().strength((d) => charge - (S.degree.get(d.id) || 1) * 8))
    .force("center", d3.forceCenter(W / 2, H / 2))
    .force("collide", d3.forceCollide().radius((d) => radius(d.id) + pad))
    .force("x", d3.forceX(W / 2).strength((d) => (S.degree.get(d.id) ? 0.04 : 0.2)))
    .force("y", d3.forceY(H / 2).strength((d) => (S.degree.get(d.id) ? 0.04 : 0.2)))
    .on("tick", () => {
      S.draw();
      const now = performance.now();
      if (!S.viewMoved && now - (S.lastFit || 0) > 400) { S.lastFit = now; fit(true); }
    })
    .on("end", () => { if (!S.viewMoved) fit(); });
  if (isStar && hubId) { const hub = S.nodes.find((n) => n.id === hubId); if (hub && !hub.pinned) { hub.fx = W / 2; hub.fy = H / 2; } }
  if (S.layout !== "force") applyLayout(S.layout, false);
  else if (S.nodes.length > HUGE_GRAPH) settle();
  else S.sim.alpha(0.9).restart();
}
function tick(lines, labels, groups, showLabels) {
  for (const el of lines) {
    const d = el.__data__;
    const x1 = d.source.x, y1 = d.source.y, dx = d.target.x - x1, dy = d.target.y - y1, len = Math.hypot(dx, dy);
    if (d.source === d.target) {                               // a loop above the node, wider for each further one
      const r = radius(d.source.id), w = r + 10 + 14 * ((d.bend - 0.16) / 0.14), h = r + 26 + 16 * ((d.bend - 0.16) / 0.14);
      el.setAttribute("d", `M${x1 + r * 0.5},${y1 - r * 0.85} C${x1 + w},${y1 - h} ${x1 - w},${y1 - h} ${x1 - r * 0.5},${y1 - r * 0.85}`);
      d.mid = [x1, y1 - h * 0.78];
      continue;
    }
    if (!(len > 0)) { el.setAttribute("d", ""); continue; }
    const b = (d.bend ?? 0.16) * len;                          // the control point, off the chord to its left
    const cx = (x1 + d.target.x) / 2 + (dy / len) * b, cy = (y1 + d.target.y) / 2 - (dx / len) * b;
    let x2 = d.target.x, y2 = d.target.y;
    const tx = x2 - cx, ty = y2 - cy, tl = Math.hypot(tx, ty);   // end at the target's rim, so the arrowhead shows
    if (tl > 0) { const r = Math.min(tl - 1, radius(d.target.id) + 2); x2 -= (tx / tl) * r; y2 -= (ty / tl) * r; }
    el.setAttribute("d", `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`);
    d.mid = [0.25 * x1 + 0.5 * cx + 0.25 * d.target.x, 0.25 * y1 + 0.5 * cy + 0.25 * d.target.y];
  }
  if (showLabels) for (const el of labels) {
    const d = el.__data__;
    const [mx, my] = d.mid || [(d.source.x + d.target.x) / 2, (d.source.y + d.target.y) / 2];
    el.setAttribute("x", mx); el.setAttribute("y", my);
  }
  for (const el of groups) { const d = el.__data__; el.setAttribute("transform", `translate(${d.x},${d.y})`); }
}
// A big graph is laid out in slices of 40 ms, so the page stays responsive; the view follows it as it spreads.
function settle() {
  if (S.big) { layoutRun(); return; }
  const gen = ++S.settleGen, sim = S.sim;
  sim.stop(); sim.alpha(0.9);
  let drawnAt = 0;
  const slice = () => {
    if (gen !== S.settleGen || sim !== S.sim) return;
    const t0 = performance.now();
    while (sim.alpha() >= sim.alphaMin() && performance.now() - t0 < 40) sim.tick();
    const done = sim.alpha() < sim.alphaMin();
    if (done || t0 - drawnAt > 400) { S.draw(); if (!done && !S.viewMoved) fit(true); drawnAt = performance.now(); }
    if (!done) setTimeout(slice, 0); else if (!S.viewMoved) fit();
  };
  setTimeout(slice, 0);
}
function syncFar(k) { svg.classed("far", S.nodes.length > BIG_GRAPH && k < FAR_ZOOM); }
function fit(instant = false, nodes = S.nodes) {
  const run = () => {
    const pts = nodes.map((n) => [n.fx ?? n.x, n.fy ?? n.y]).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
    if (!pts.length) return;
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const [W, H] = size(), PAD = 70;
    const k = Math.min(2, Math.max(S.zoom.scaleExtent()[0], Math.min(W / (maxX - minX + PAD * 2 || 1), H / (maxY - minY + PAD * 2 || 1))));
    const to = d3.zoomIdentity.translate(W / 2 - ((minX + maxX) / 2) * k, H / 2 - ((minY + maxY) / 2) * k).scale(k);
    instant ? svg.call(S.zoom.transform, to) : svg.transition().duration(450).call(S.zoom.transform, to);
  };
  instant ? run() : requestAnimationFrame(() => requestAnimationFrame(run));
}
// a click is a press with no movement; a drag pins the node where it is dropped
function drag() {
  return d3.drag()
    .on("start", (ev, d) => { d.fx = d.x; d.fy = d.y; d._sx = ev.x; d._sy = ev.y; d._moved = false; d._heated = false; })
    .on("drag", (ev, d) => {
      d.fx = ev.x; d.fy = ev.y;
      if (!d._moved && Math.hypot(ev.x - d._sx, ev.y - d._sy) > 3) d._moved = true;
      if (d._moved && !d._heated) { S.settleGen += 1; S.viewMoved = true; S.sim.alphaTarget(S.nodes.length > BIG_GRAPH ? 0.08 : 0.3).restart(); d._heated = true; }
    })
    .on("end", (ev, d) => {
      if (d._heated && !ev.active) S.sim.alphaTarget(0);
      if (d._moved) { d.pinned = true; d.fx = ev.x; d.fy = ev.y; }
      else { if (!d.pinned) { d.fx = null; d.fy = null; } select(d.id); }
    });
}

// ------------------------------------------------------------------------------------------ layouts
function adjacency() {
  const out = new Map(S.nodes.map((n) => [n.id, []])), inn = new Map(S.nodes.map((n) => [n.id, []])), und = new Map(S.nodes.map((n) => [n.id, []]));
  for (const e of S.links) {
    const s = e.source.id ?? e.source, t = e.target.id ?? e.target;
    out.get(s)?.push(t); inn.get(t)?.push(s); und.get(s)?.push(t); und.get(t)?.push(s);
  }
  return { out, inn, und };
}
function hierarchical() {
  const { out, inn } = adjacency(), [W, H] = size();
  const level = new Map(), queue = [];
  const order = [...S.nodes].sort((a, b) => (out.get(b.id).length - inn.get(b.id).length) - (out.get(a.id).length - inn.get(a.id).length));
  const seed = (id) => { if (!level.has(id)) { level.set(id, 0); queue.push(id); } };
  const drain = () => { while (queue.length) { const id = queue.shift(); for (const t of out.get(id)) if (!level.has(t)) { level.set(t, level.get(id) + 1); queue.push(t); } } };
  order.filter((n) => !inn.get(n.id).length).forEach((n) => seed(n.id)); drain();
  for (const n of order) if (!level.has(n.id)) { seed(n.id); drain(); }
  const layers = new Map();
  for (const [id, l] of level) (layers.get(l) || layers.set(l, []).get(l)).push(id);
  const pos = new Map(), perRow = Math.max(12, Math.ceil(Math.sqrt(S.nodes.length) * 2)), dx = 110;
  let y = 0;
  [...layers.keys()].sort((a, b) => a - b).forEach((l) => {
    const ids = layers.get(l);
    if (l > 0) { const bary = (id) => { const xs = inn.get(id).map((p) => pos.get(p)).filter(Boolean).map((p) => p[0]); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0; }; ids.sort((a, b) => bary(a) - bary(b)); }
    for (let r = 0; r * perRow < ids.length; r++) {
      const row = ids.slice(r * perRow, (r + 1) * perRow), left = W / 2 - ((row.length - 1) * dx) / 2 + (r % 2 ? dx / 2 : 0);
      row.forEach((id, i) => pos.set(id, [left + i * dx, y]));
      y += 70;
    }
    y += 80;
  });
  for (const p of pos.values()) p[1] += H / 2 - y / 2;
  return pos;
}
function radial() {
  const { und } = adjacency(), [W, H] = size();
  const root = S.selected && und.has(S.selected) ? S.selected : [...S.nodes].sort((a, b) => und.get(b.id).length - und.get(a.id).length)[0].id;
  const depth = new Map([[root, 0]]), parent = new Map(), queue = [root];
  while (queue.length) { const id = queue.shift(); for (const t of und.get(id)) if (!depth.has(t)) { depth.set(t, depth.get(id) + 1); parent.set(t, id); queue.push(t); } }
  const outer = Math.max(0, ...depth.values()) + 1;
  for (const n of S.nodes) if (!depth.has(n.id)) depth.set(n.id, outer);
  const rings = new Map();
  for (const [id, d] of depth) (rings.get(d) || rings.set(d, []).get(d)).push(id);
  const pos = new Map([[root, [W / 2, H / 2]]]), angle = new Map([[root, 0]]);
  [...rings.keys()].sort((a, b) => a - b).forEach((d) => {
    if (!d) return;
    const ids = rings.get(d).sort((a, b) => (angle.get(parent.get(a)) ?? 0) - (angle.get(parent.get(b)) ?? 0));
    const r = Math.max(d * 150, (ids.length * 34) / (2 * Math.PI));
    ids.forEach((id, i) => { const a = (2 * Math.PI * i) / ids.length; angle.set(id, a); pos.set(id, [W / 2 + r * Math.cos(a), H / 2 + r * Math.sin(a)]); });
  });
  return pos;
}
function applyLayout(kind, refit = true) {
  if (!S.nodes.length || !S.sim) return;
  S.settleGen += 1;
  if (kind === "force") {
    for (const n of S.nodes) if (!n.pinned) { n.fx = null; n.fy = null; }
    S.viewMoved = !refit;
    if (S.nodes.length > HUGE_GRAPH) settle(); else { S.sim.alpha(0.9).restart(); if (refit) setTimeout(() => fit(), 900); }
    return;
  }
  const pos = kind === "hierarchical" ? hierarchical() : radial();
  S.sim.stop();
  for (const n of S.nodes) { const p = pos.get(n.id); if (p) { n.pinned = false; n.fx = n.x = p[0]; n.fy = n.y = p[1]; n.vx = n.vy = 0; } }
  S.draw();
  if (refit) fit();
}

// ------------------------------------------------------------------------------------------ labels, types and the node
function renderLegend() {
  const pill = (attr, key, label, count, colour, pressed, neutral) =>
    `<button class="gx-pill" type="button" ${attr}="${esc(key)}" ${neutral ? 'data-tone="neutral"' : `style="--pill:${colour}"`} aria-pressed="${pressed}">${esc(label)} <span class="gx-pill-count">(${count.toLocaleString()})</span></button>`;
  $("kinds").innerHTML = pill("data-l", "*", "*", G.doc.nodes.length, "", !S.kind, true)
    + [...G.labels].sort((a, b) => b[1] - a[1]).map(([l, n]) => pill("data-l", l, l, n, colourForKind(l), S.kind === l)).join("");
  $("rels").innerHTML = pill("data-t", "*", "*", G.doc.links.length, "", !S.rel, true)
    + [...G.rels].sort((a, b) => b[1] - a[1]).map(([t, n]) => pill("data-t", t, t, n, colourForEdge(t), S.rel === t)).join("");
  $("kinds").querySelectorAll(".gx-pill").forEach((b) => (b.onclick = () => {
    const l = b.dataset.l === "*" ? null : b.dataset.l;
    S.kind = S.kind === l ? null : l;
    S.sources = S.kind === PROVENANCE_KIND;          // the sources show when asked for by their label
    S.extra.clear();
    renderLegend(); draw();
  }));
  $("rels").querySelectorAll(".gx-pill").forEach((b) => (b.onclick = () => {
    const t = b.dataset.t === "*" ? null : b.dataset.t;
    S.rel = S.rel === t ? null : t;
    if (S.rel === PROVENANCE_EDGE) S.sources = true;
    renderLegend(); draw();
  }));
}
function showLegend() {
  $("legend").hidden = false; $("record").hidden = true; $("inspector-back").hidden = true;
  $("inspector-title").textContent = "Labels, types & props";
}
function clearSelection() {
  S.selected = null;
  if (S.big) S.draw();
  d3.selectAll(".gx-node").classed("selected", false);
  $("locate").disabled = true;
  showLegend();
}
function select(id) {
  S.selected = id;
  if (S.big) S.draw();
  d3.selectAll(".gx-node").classed("selected", (d) => d.id === id);
  $("locate").disabled = false;
  showDetails(G.byId.get(id));
}
// a node not on the canvas (a source, a node of another label) is brought onto it
function goTo(id) {
  if (!G.byId.has(id)) { const l = G.doc.links.find((e) => e.id === id); if (l) showDetails(l); return; }
  if (!S.nodes.some((n) => n.id === id)) { S.hidden.delete(id); S.extra.add(id); draw(); }
  select(id);
  const n = S.nodes.find((x) => x.id === id);
  setTimeout(() => { if (n && Number.isFinite(n.x)) { S.viewMoved = true; svg.transition().duration(450).call(S.zoom.translateTo, n.x, n.y); } }, S.sim && S.sim.alpha() > 0.1 ? 900 : 0);
}
function expand(id) {
  if (G.entry.windowed && bundle?.drawn) {           // a bundle's window: draw around this record's source
    const s = bundle.b.sourceOf(id);
    if (s != null && !(s === bundle.centre && bundle.from === 0)) { drawWindow({ centre: s, from: 0, focus: id }); return; }
  }
  const near = [...(G.out.get(id) || []), ...(G.inn.get(id) || [])].slice(0, 50);
  for (const e of near) for (const n of [e.source, e.target]) { S.hidden.delete(n); S.extra.add(n); }
  S.extra.add(id);
  draw();
  select(id);
}
const COPY = `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8 V5 a1 1 0 0 0 -1 -1 H5 a1 1 0 0 0 -1 1 V15 a1 1 0 0 0 1 1 H8"/></svg>`;
function showDetails(rec) {
  if (!rec) return;
  const p = rec.props || {};
  const isLink = "source" in rec && "target" in rec;
  const head = isLink
    ? `<div class="gx-record-head"><span class="gx-pill" style="--pill:${colourForEdge(rec.type)}">${esc(rec.type)}</span></div>
       <p><a href="#" data-id="${esc(rec.source)}">${esc(name(G.byId.get(rec.source)))}</a> → <a href="#" data-id="${esc(rec.target)}">${esc(name(G.byId.get(rec.target)))}</a></p>`
    : `<div class="gx-record-head"><span class="gx-pill" style="--pill:${colourForKind(rec.label)}">${esc(rec.label)}</span><span class="gx-record-label">${esc(name(rec))}</span></div>`;
  const entries = [["id", rec.id], ...Object.entries(p).filter(([k]) => k !== "evidence")];
  const rows = entries.map(([k, v], i) => `<tr><td>${esc(k)}</td><td>${idLinks(value(v))}</td><td class="act"><button class="gx-copy" data-i="${i}" title="Copy">${COPY}</button></td></tr>`).join("");
  const ev = (p.evidence || []).map((e) => {
    const src = G.byId.get(e.source);
    const where = [src ? name(src) : e.source, src?.props?.year, e.locator].filter(Boolean).map(esc).join(" · ");
    return `<blockquote class="gx-quote-block">${e.quote ? esc(e.quote) : `<span class="gx-subtle">No quote kept.</span>`}<div class="gx-meta">${src ? `<a href="#" data-id="${esc(src.id)}">${where}</a>` : where}</div></blockquote>`;
  }).join("");
  let neighbours = "";
  if (!isLink) {
    const items = [...(G.out.get(rec.id) || []).map((e) => [`${e.type} →`, e.target]), ...(G.inn.get(rec.id) || []).map((e) => [`← ${e.type}`, e.source])];
    neighbours = `<section class="gx-inspector-section"><h3>Neighbours <span class="gx-count">${items.length}</span></h3><ul class="gx-neighbours">${
      items.slice(0, 120).map(([rel, id]) => { const n = G.byId.get(id); return `<li><span class="dot" style="background:${colourForKind(n?.label)}"></span><a href="#" data-id="${esc(id)}">${esc(trunc(name(n), 60))}</a><span class="gx-rel">${esc(rel)}</span></li>`; }).join("")}
      ${items.length > 120 ? `<li class="gx-muted">… ${items.length - 120} more</li>` : ""}</ul></section>`;
  }
  const node = S.nodes.find((n) => n.id === rec.id);
  $("record").innerHTML = `${head}
    <table class="gx-props"><thead><tr><th>Key</th><th>Value</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    ${ev ? `<section class="gx-inspector-section"><h3>Provenance <span class="gx-count">${(p.evidence || []).length}</span></h3>${ev}</section>` : ""}
    ${neighbours}
    ${isLink ? "" : `<footer class="gx-inspector-actions"><button class="gx-btn gx-btn-tiny" id="act-expand">Expand neighbourhood</button>
      <button class="gx-btn gx-btn-tiny" id="act-focus">Focus</button><button class="gx-btn gx-btn-tiny" id="act-pin" aria-pressed="${!!node?.pinned}">${node?.pinned ? "Unpin" : "Pin"}</button>
      <button class="gx-btn gx-btn-tiny" id="act-hide" title="Remove from this view (the graph is not changed)">Hide</button></footer>`}`;
  wireIds($("record"));
  $("record").querySelectorAll(".gx-copy").forEach((b) => (b.onclick = () => { const v = entries[Number(b.dataset.i)][1]; navigator.clipboard?.writeText(typeof v === "string" ? v : JSON.stringify(v)); }));
  if (!isLink) {
    $("act-expand").onclick = () => expand(rec.id);
    $("act-focus").onclick = () => focus(rec.id);
    $("act-pin").onclick = () => { const n = S.nodes.find((x) => x.id === rec.id); if (n) { n.pinned = !n.pinned; if (n.pinned) { n.fx = n.x; n.fy = n.y; } else { n.fx = n.fy = null; } showDetails(rec); } };
    $("act-hide").onclick = () => { S.hidden.add(rec.id); S.extra.delete(rec.id); clearSelection(); draw(); };
  }
  $("legend").hidden = true; $("record").hidden = false; $("inspector-back").hidden = false;
  $("inspector-title").textContent = isLink ? "Relationship details" : "Node details";
  if ($("grid").classList.contains("inspector-off")) panel("inspector", true);
}
function focus(id) {
  const ids = new Set([id, ...[...(G.out.get(id) || []), ...(G.inn.get(id) || [])].flatMap((e) => [e.source, e.target])]);
  S.viewMoved = true;
  fit(false, S.nodes.filter((n) => ids.has(n.id)));
}
$("inspector-back").onclick = () => clearSelection();

// ------------------------------------------------------------------------------------------ the answer card
function card(title, html) {
  $("card-title").textContent = title;
  $("card-body").innerHTML = html;
  $("card").hidden = false;
  $("card").classList.remove("collapsed");
  wireIds($("card-body"));
}
function markMatches() {
  if (S.big) { S.draw(); return; }
  const m = S.match;
  d3.selectAll(".gx-node").classed("match", (d) => !!m && m.has(d.id)).classed("dim", (d) => !!m && !m.has(d.id));
  d3.selectAll("#links path").classed("dim", (d) => !!m && !(m.has(d.source.id ?? d.source) && m.has(d.target.id ?? d.target)));
}
$("card-close").onclick = () => { $("card").hidden = true; S.match = null; markMatches(); };
$("card-collapse").onclick = () => $("card").classList.toggle("collapsed");

async function search(q) {
  if (G?.entry.windowed && bundle?.drawn) return searchBundle(q);
  leaveSchema();
  card("Matching evidence", `<p class="gx-meta">Searching…</p>`);
  try {
    const r = await scale("search", { query: q, k: 25 });
    if (r.error) throw new Error(r.error);
    const hits = r.hits || [];
    S.match = new Set(hits.flatMap((h) => [h.record, ...(h.also || []).map((a) => a.record)]));
    const missing = [...S.match].filter((id) => G.byId.has(id) && !S.nodes.some((n) => n.id === id));
    if (missing.length) { missing.forEach((id) => S.extra.add(id)); draw(); } else markMatches();
    const shown = S.nodes.filter((n) => S.match.has(n.id));               // the view goes to what matched
    if (shown.length) setTimeout(() => { S.viewMoved = true; fit(false, shown); }, missing.length ? 900 : 0);
    card(`Matching evidence · ${hits.length}`, (hits.length ? hits.map((h) => `<div class="gx-hit" data-id="${esc(h.record)}">
        <div class="gx-quote">“${esc(h.text)}”</div>
        <div class="gx-meta">${esc(h.label || "")}: ${esc(h.record_name || h.record)}${h.also?.length ? ` <span title="${esc(h.also.slice(0, 20).map((a) => `${a.label || ""}: ${a.record_name || a.record}`).join("\n") + (h.also.length > 20 ? `\n… and ${h.also.length - 20} more` : ""))}">and ${h.also.length} more citing it</span>` : ""} · ${esc(name(G.byId.get(h.source)))}${h.source_year ? ` (${esc(h.source_year)})` : ""}, ${esc(h.locator || "")} · ${Number(h.score || 0).toFixed(3)}</div></div>`).join("")
      : `<p>No evidence matches “${esc(q)}”.</p>`) + (!ownKey && !(SERVICE && token) ? `<p class="gx-meta">These are the quotes that
        match, found in this browser. ${SERVICE ? `<a href="#" role="button" data-signin="search">Sign in with GitHub</a> to have
        questions answered in words (free, no API key), or <a href="#" role="button" data-ownkey="search">use your own API
        key</a>.` : `To have questions answered in words, <a href="#" role="button" data-ownkey="search">use your own API
        key</a>.`}</p>` : ""));
    $("card-body").querySelectorAll(".gx-hit").forEach((el) => (el.onclick = () => goTo(el.dataset.id)));
    $("card-body").querySelector("[data-ownkey]")?.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); openOwnKeyMenu(); });
  } catch (err) { card("Matching evidence", `<p class="gx-card-warn">${esc(err.message)}</p>`); }
}
// ------------------------------------------------------------------------------------------ answers in words
// The page keeps the service's session token (sessionStorage, or localStorage when it was saved before) and the sign-in
// under way (this tab's sessionStorage only). Each storage is tried on its own, so one that is blocked hides no other.
const inStore = (kind, f) => { try { return f(window[kind]); } catch { return undefined; } };
const store = {
  get: (k) => inStore("localStorage", (s) => s.getItem(k)) || inStore("sessionStorage", (s) => s.getItem(k)) || "",
  set(k, v, keep) {              // true when it was kept
    const ok = inStore(keep ? "localStorage" : "sessionStorage", (s) => { if (v) s.setItem(k, v); else s.removeItem(k); return true; });
    if (!keep) inStore("localStorage", (s) => s.removeItem(k));
    return ok === true;
  },
  take(k) {                      // read once, from this tab only
    const v = inStore("sessionStorage", (s) => s.getItem(k)) || "";
    inStore("sessionStorage", (s) => s.removeItem(k));
    return v;
  } };
// Egress (egress.js, docs/private-data.md): nothing drawn from a graph whose records are not all public goes to a model
// provider or to the service unless the reader approves that destination for those access levels here. Approvals
// live in this page only (never stored): a reload denies again.
const egressApprovals = new Set();
function graphLevels() {                          // the open graph's access levels that are not public ([]: all public)
  const B = G.base || G;
  if (!B.closedLevels) B.closedLevels = closedLevels(B.doc);
  return B.closedLevels;
}
function egressCard(err, levels, destination, retry) {
  card("Answer", `<p class="gx-card-warn">${esc(err.message || err)}</p>
    <button type="button" class="gx-btn gx-btn-tiny" id="egress-approve" title="Approve for this page only; a reload denies again">Approve sending ${esc(levels.join(", "))} records to it from this page</button>`);
  $("egress-approve").onclick = () => { egressApprove(levels, destination, egressApprovals); retry(); };
}
// A question is answered by the service (the query-graph skill over this graph, within the service's budget) for a
// signed-in reader; anyone else sees the matching evidence, found in this browser.
async function ask(question) {
  leaveSchema();
  const levels = graphLevels(), check = egressGate(levels, "service", "the SCALE service", egressApprovals);
  try { check(); } catch (err) { return egressCard(err, levels, "service", () => ask(question)); }
  card("Answer", `<p class="gx-meta">Answering… the service runs query plans over this graph.</p>`);
  try {
    check();                                        // the whole graph goes to the service: only where it may go
    const r = await call("POST", "/ask", { graph: (G.base || G).doc, question });
    answerCard(r, `about ${Number(r.tokens || 0).toLocaleString()} tokens, from the service's budget`);
  } catch (err) { card("Answer", `<p class="gx-card-warn">${esc(err.message || err)}</p>`); search(question); }
}
// The answer, the query plans that gave its rows, and what it cost (the service's answer and the page's own alike).
function answerCard(r, cost) {
  card("Answer", `${idLinks(md(r.answer || "No answer."))}
    ${r.stopped ? `<p class="gx-card-warn">Stopped early: ${esc(r.stopped)}.</p>` : ""}
    ${(r.plans || []).length ? `<details><summary>How it was answered (${r.plans.length} plan${r.plans.length > 1 ? "s" : ""})</summary>${r.plans.map((p, i) =>
      `<p class="gx-meta">Plan ${i + 1}: ${esc(p.status || "?")}, ${p.rows ?? 0} rows</p><pre>${esc(JSON.stringify(p.plan, null, 1))}</pre>`).join("")}</details>` : ""}
    <p class="gx-meta">${esc(cost)}</p>`);
}

// ------------------------------------------------------------------------------------------ answers with the reader's key
// The service's question loop (service/scaleweb/agent.py, answer), run in this page: the model, called from this
// browser with the reader's key, follows the query-graph skill (data/query-graph.md, written by `scale site build`)
// through describe, stats, search and query, which run SCALE's own code here (worker.js). The reader picks the
// provider: Anthropic (its Messages API), or OpenAI or OpenRouter (OpenAI's chat completions format). The key is kept
// in this tab (sessionStorage), or on this device (localStorage) when the reader asks, and is sent only to the chosen
// provider's API; never to the service, GitHub or anyone else. A reader's key is spent on their own account.
const ANTHROPIC = "https://api.anthropic.com/v1/messages";
const OWN_MODELS = [["claude-opus-5-5", "Claude Opus 5.5"], ["claude-sonnet-5-5", "Claude Sonnet 5.5"], ["claude-haiku-5-5", "Claude Haiku 5.5"]];
const PROVIDERS = {
  anthropic: { name: "Anthropic", host: "api.anthropic.com", hint: "sk-ant-...", keys: "https://platform.claude.com/settings/keys" },
  openai: { name: "OpenAI", host: "api.openai.com", hint: "sk-...", keys: "https://platform.openai.com/api-keys",
    url: "https://api.openai.com/v1/chat/completions", model: "gpt-5", limit: "max_completion_tokens" },
  openrouter: { name: "OpenRouter", host: "openrouter.ai", hint: "sk-or-...", keys: "https://openrouter.ai/settings/keys",
    url: "https://openrouter.ai/api/v1/chat/completions", model: "openai/gpt-5", limit: "max_tokens" },
};
// A question runs until the model answers: no token budget, no turn limit and no tool result cut short, so that the
// answer rests on every row the model asked for. The reader may stop it (Answer now), and the model then answers from
// the rows it has; the cost line says what it spent.
const WRAP_UP = "The reader asked for the answer now. Call no more tools: write the answer from the rows returned so far, "
  + "and say in the Limits line what was left unexplored.";
let answerNow = false;                                    // set by the progress card's Answer now button
// The reader's keys: a list (scale-own-keys, JSON) and the one in use (scale-own-active), kept in this tab, or on this
// device when the reader asks. Each key is {id, provider, key, model, workspace}; the one in use sets ownKey and the rest.
const freshModel = (provider) => provider === "anthropic" ? OWN_MODELS[0][0] : PROVIDERS[provider].model;
const goodModel = (provider, model) => provider === "anthropic" ? OWN_MODELS.some(([m]) => m === model)
  : !!model && !OWN_MODELS.some(([m]) => m === model);
let ownKeys = [], ownActive = "", ownKeep = false;
try { ownKeys = JSON.parse(store.get("scale-own-keys") || "[]").filter((k) => k && PROVIDERS[k.provider] && k.key); } catch { ownKeys = []; }
for (const k of ownKeys) if (!goodModel(k.provider, k.model)) k.model = freshModel(k.provider);
ownActive = store.get("scale-own-active");
ownKeep = !!inStore("localStorage", (s) => s.getItem("scale-own-keys"));
const oldKey = store.get("scale-own-key");                // a key saved before the page kept a list
if (oldKey && !ownKeys.length) {
  const provider = PROVIDERS[store.get("scale-own-provider")] ? store.get("scale-own-provider") : "anthropic";
  const model = store.get("scale-own-model");
  ownKeep = !!inStore("localStorage", (s) => s.getItem("scale-own-key"));
  ownKeys = [{ id: "k1", provider, key: oldKey, model: goodModel(provider, model) ? model : freshModel(provider), workspace: "" }];
  ownActive = "k1";
}
for (const k of ["scale-own-key", "scale-own-provider", "scale-own-model"]) store.set(k, "", false);
let ownKey = "", ownProvider = "anthropic", ownModel = OWN_MODELS[0][0], ownWorkspace = "";
function useOwnKeys() {                                   // the key in use, from the list; saved where the reader chose
  if (!ownKeys.some((k) => k.id === ownActive)) ownActive = ownKeys[0]?.id || "";
  const a = ownKeys.find((k) => k.id === ownActive);
  ownKey = a?.key || ""; ownProvider = a?.provider || "anthropic"; ownModel = a?.model || OWN_MODELS[0][0]; ownWorkspace = a?.workspace || "";
  store.set("scale-own-keys", ownKeys.length ? JSON.stringify(ownKeys) : "", ownKeep);
  store.set("scale-own-active", ownActive, ownKeep);
  if (ownKeep) inStore("sessionStorage", (s) => { s.removeItem("scale-own-keys"); s.removeItem("scale-own-active"); });   // one copy only
}
useOwnKeys();
const labelOf = (k) => k.provider === "anthropic" ? OWN_MODELS.find(([m]) => m === k.model)[1] : `${k.model} via ${PROVIDERS[k.provider].name}`;
const modelLabel = () => labelOf({ provider: ownProvider, model: ownModel });
const tail = (key) => `…${key.slice(-4)}`;
let skillText = null;
const OWN_TOOLS = [
  { name: "describe", description: "The schema: labels and edge types with their properties (scale describe).",
    input_schema: { type: "object", properties: { names: { type: "array", items: { type: "string" } } }, required: [] } },
  { name: "stats", description: "The graph's units in use per property and its link patterns (scale graph stats).",
    input_schema: { type: "object", properties: {}, required: [] } },
  { name: "search", description: "Search the graph's evidence by words and meaning (scale graph search); hits lead to the records that cite them, one hit per quote (the other records citing it in `also`). Formulas keep their case: CO is carbon monoxide, Co cobalt.",
    input_schema: { type: "object", properties: { query: { type: "string" }, k: { type: "integer" }, published_before: { type: "string" } }, required: ["query"] } },
  { name: "query", description: "Run a graph query plan (scale graph query): match, steps, where, return, evidence, order_by, limit, text, published_before. Returns rows, the kind of answer and the coverage. A numeric filter states its unit; add convert: true to also compare values in another unit of the same quantity, converted exactly (temperature K, °C, °F; absolute pressure; time), e.g. {prop: temperature, op: le, value: 250, unit: °C, convert: true}. Text filters ignore case except formulas (Co is cobalt, CO carbon monoxide).",
    input_schema: { type: "object", properties: { plan: { type: "object" } }, required: ["plan"] } },
];
const WORKING_HERE = "\n\n# Working here\n\nYou answer in a web page, through the tools given (no shell). The graph is the one the user is viewing. "
  + "Write the answer in Markdown: every factual sentence from returned rows, citing record ids and evidence locators in brackets, then a "
  + "**Limits** line. Keep your own extrapolations in a separate, labelled paragraph. Tool results come back whole: "
  + "run every plan the question needs, and base the answer on all the relevant rows they return.";
// The cache breakpoints on the conversation: the newest two user messages, so each turn reads all that came before
// from the cache and writes only what is new (with the system prompt's, three of the four breakpoints allowed).
function markCache(messages) {
  for (const m of messages) if (Array.isArray(m.content)) for (const b of m.content) delete b.cache_control;
  messages.filter((m) => m.role === "user" && Array.isArray(m.content) && m.content.length).slice(-2)
    .forEach((m) => { m.content[m.content.length - 1].cache_control = { type: "ephemeral" }; });
}
const whole = (obj) => typeof obj === "string" ? obj : JSON.stringify(obj);

async function claude(body) {
  const fallback = !body.model.startsWith("claude-haiku");          // a declined request is answered by another model
  const r = await fetch(ANTHROPIC, { method: "POST", headers: {
    "content-type": "application/json", "x-api-key": ownKey, "anthropic-version": "2023-06-01",
    "anthropic-dangerous-direct-browser-access": "true", ...(ownWorkspace ? { "anthropic-workspace-id": ownWorkspace } : {}), ...(fallback ? { "anthropic-beta": "server-side-fallback-2026-07-01" } : {}) },
    body: JSON.stringify(fallback ? { ...body, fallbacks: "default" } : body) });
  const j = await r.json().catch(() => ({}));
  if (r.ok) return j;
  const why = j.error?.message || `HTTP ${r.status}`;
  if (r.status === 401) throw new Error(`Anthropic did not accept the API key (${why}). Check it under Your API key.`);
  if (r.status === 429) throw new Error(`Your API account's rate limit was reached; ask again in a minute (${why}).`);
  if (/usage limit|spend limit|credit balance/i.test(why)) throw new Error(`Anthropic's API answered: ${why} The spend limit is set in the Claude Console (Settings, Limits, or the workspace's own limits under Settings, Workspaces), or switch to another key under Your API key.`);
  if (/prompt is too long|context window|too many tokens/i.test(why)) throw new Error(`Anthropic's API answered: ${why} The work so far outgrew what the model can read at once: ask a narrower question, or press Answer now (or Finish now) earlier.`);
  if (/anthropic-workspace-id|scoped to a workspace/i.test(why)) throw new Error(`Anthropic's API answered: ${why} Under Your API key, edit the key and give its Workspace ID (Claude Console, Settings, Workspaces), or use a key created inside a workspace.`);
  throw new Error(`Anthropic's API answered: ${why}`);
}
// OpenAI's chat completions format, which OpenAI and OpenRouter both take: the same tools, as functions.
async function chat(provider, body) {
  const p = PROVIDERS[provider];
  const r = await fetch(p.url, { method: "POST", headers: { "content-type": "application/json", "Authorization": `Bearer ${ownKey}` },
    body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (r.ok && !j.error) return j;
  const why = j.error?.message || `HTTP ${r.status}`;
  if (r.status === 401) throw new Error(`${p.name} did not accept the API key (${why}). Check it under Your API key.`);
  if (r.status === 402) throw new Error(`Your ${p.name} account has no credit left for this (${why}).`);
  if (r.status === 429) throw new Error(`Your API account's rate limit was reached; ask again in a minute (${why}).`);
  if (r.status === 404 || /model/i.test(why)) throw new Error(`${p.name} answered: ${why}. Check the model name under Your API key.`);
  throw new Error(`${p.name}'s API answered: ${why}`);
}
async function ownTool(name, a, plans) {
  if (name === "describe") return scale("describe", { names: a.names || [], domain: "catalysis" });
  if (name === "stats") return scale("stats", {});
  if (name === "search") return scale("search", { query: String(a.query || ""), k: Number(a.k) || 10, ...(a.published_before ? { published_before: a.published_before } : {}) });
  if (name === "query") {
    const res = await scale("query", { plan: a.plan });
    plans.push({ plan: a.plan, status: res.status || res.error, rows: res.row_count || 0 });
    return res;
  }
  throw new Error(`unknown tool ${name}`);
}
// One tool-use loop for every provider and every task (a question, a curation): the model runs the tools until it
// writes its answer, or calls the stop tool (curation's done), or the reader asks it to finish (stopNow), when wrapUp
// is said to it beside the last results and its next reply ends the loop. No token budget, no turn limit and no tool
// result cut short. In Anthropic's format the conversation is cached as it grows (markCache), the tools and the
// system prompt staying the same throughout (the last turn keeps the tools, so the cache holds); in the chat
// completions format (OpenAI, OpenRouter) the provider caches the repeated prefix itself.
async function modelLoop({ system, tools, first, run, stop = "", wrapUp, stopNow, progress, alive, gate = () => {} }) {
  const provider = ownProvider, model = ownModel;
  const out = { text: "", stopInput: null, stopped: "", inTok: 0, cacheTok: 0, outTok: 0 };
  const chatTools = tools.map((t) => ({ type: "function", function: { name: t.name, description: t.description, parameters: t.input_schema } }));
  const messages = provider === "anthropic" ? [{ role: "user", content: first }]
    : [{ role: "system", content: system }, { role: "user", content: first }];
  const sys = [{ type: "text", text: system, cache_control: { type: "ephemeral" } }];
  for (let turn = 0; ; turn++) {
    const last = !!out.stopped;                             // the reader asked to finish: this reply ends the loop
    let calls, cut;                                         // [{id, name, input}], and whether the reply ran out of room
    if (provider === "anthropic") {
      gate();                                               // egress: throws before a request it may not send
      markCache(messages);
      const r = await claude({ model, max_tokens: OWN_OUTPUT, system: sys, tools, messages });
      if (!alive()) return null;
      const u = r.usage || {};
      out.inTok += (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0);
      out.cacheTok += u.cache_read_input_tokens || 0; out.outTok += u.output_tokens || 0;
      const blocks = r.content || [];
      messages.push({ role: "assistant", content: blocks });   // as returned, thinking blocks included
      out.text = blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n").trim();
      if (r.stop_reason === "refusal") { out.stopped = "the model declined"; return out; }
      calls = blocks.filter((b) => b.type === "tool_use").map((c) => ({ id: c.id, name: c.name, input: c.input || {} }));
      cut = r.stop_reason === "max_tokens";
    } else {
      gate();
      const r = await chat(provider, { model, messages, tools: chatTools, [PROVIDERS[provider].limit]: OWN_OUTPUT });
      if (!alive()) return null;
      out.inTok += r.usage?.prompt_tokens || 0; out.cacheTok += r.usage?.prompt_tokens_details?.cached_tokens || 0;
      out.outTok += r.usage?.completion_tokens || 0;
      const choice = r.choices?.[0] || {}, msg = choice.message || { role: "assistant", content: "" };
      messages.push(msg);                                   // as returned (OpenRouter's reasoning details included)
      out.text = (typeof msg.content === "string" ? msg.content : (msg.content || []).map((c) => c.text || "").join("\n")).trim();
      if (msg.refusal || choice.finish_reason === "content_filter") { out.stopped = "the model declined"; out.text ||= msg.refusal || ""; return out; }
      calls = (msg.tool_calls || []).filter((c) => c.type === "function" || c.function).map((c) => {
        let input = {}; try { input = JSON.parse(c.function.arguments || "{}"); } catch { /* answered as an error below */ input = null; }
        return { id: c.id, name: c.function.name, input };
      });
      cut = choice.finish_reason === "length";
    }
    const ending = stop && calls.find((c) => c.name === stop);
    if (ending) { out.stopInput = ending.input || {}; return out; }
    if (!calls.length || last) { if (cut) out.stopped ||= "the reply ran out of room"; return out; }
    progress(`Turn ${turn + 1}: ${calls.map((c) => c.name).join(", ")}`);
    const results = [];
    for (const c of calls) {
      let content, error = false;
      try {
        if (c.input === null) throw new Error("the tool call's arguments were not valid JSON");
        content = whole(await run(c.name, c.input));
      } catch (err) { content = `Error: ${err.message || err}`; error = true; }
      if (!alive()) return null;
      results.push(provider === "anthropic" ? { type: "tool_result", tool_use_id: c.id, content, ...(error ? { is_error: true } : {}) }
        : { role: "tool", tool_call_id: c.id, content });
    }
    if (stopNow()) out.stopped = "finished early, on request";
    if (provider === "anthropic") {
      if (out.stopped) results.push({ type: "text", text: wrapUp });
      messages.push({ role: "user", content: results });
    } else {
      messages.push(...results);
      if (out.stopped) messages.push({ role: "user", content: wrapUp });
    }
  }
}
const OWN_OUTPUT = 32000;                                 // the most one reply may write (the answer included)
const tokenLine = (inTok, cacheTok, outTok) =>
  `${inTok.toLocaleString()} input (${cacheTok.toLocaleString()} read from the cache) and ${outTok.toLocaleString()} output tokens`;
const accountLine = (r) => `${tokenLine(r.inTok, r.cacheTok, r.outTok)} on your API account`;
// A question, by the query-graph skill, over the graph on the canvas.
async function askOwnKey(question) {
  leaveSchema();
  const g = G, modelName = modelLabel();
  // egress: the tool results are drawn from this graph; a graph with closed records reaches the provider only once the
  // reader approves it for their levels (checked here and again before every request of the loop)
  const levels = graphLevels(), destination = `provider:${ownProvider}`;
  const check = egressGate(levels, destination, PROVIDERS[ownProvider].name, egressApprovals);
  try { check(); } catch (err) { return egressCard(err, levels, destination, () => askOwnKey(question)); }
  answerNow = false;
  const progress = (line) => {
    if (G !== g) return;
    card("Answer", `<p class="gx-meta">Answering with ${esc(modelName)} and your API key; the query plans run in this browser.</p>${line ? `<p class="gx-meta">${esc(line)}</p>` : ""}
      <button type="button" class="gx-btn gx-btn-tiny" id="answer-now"${answerNow ? " disabled" : ""} title="Stop running plans and answer from the rows returned so far">${answerNow ? "Answering from the rows so far…" : "Answer now"}</button>`);
    const b = $("answer-now");
    if (b) b.onclick = () => { answerNow = true; b.disabled = true; b.textContent = "Answering from the rows so far…"; };
  };
  progress("");
  const plans = [];
  try {
    if (skillText === null) {
      const s = await fetch("data/query-graph.md");
      if (!s.ok) throw new Error(`data/query-graph.md: HTTP ${s.status}`);
      skillText = await s.text();
    }
    const r = await modelLoop({ system: skillText + WORKING_HERE, tools: OWN_TOOLS, first: question,
      run: (name, input) => ownTool(name, input, plans), wrapUp: WRAP_UP, stopNow: () => answerNow, progress, alive: () => G === g,
      gate: check });
    if (!r) return;                                         // another graph was opened meanwhile
    const stopped = r.stopped === "finished early, on request" ? ANSWERED_EARLY : r.stopped;
    answerCard({ answer: r.text, plans, stopped }, accountLine(r));
  } catch (err) {
    if (G === g) card("Answer", `<p class="gx-card-warn">${esc(err.message || err)}</p>`);
  }
}
const ANSWERED_EARLY = "you asked for the answer before the model had finished; it answered from the rows returned so far";
function renderOwnKey() {
  const btn = $("ownkey-btn");
  $("ownkey-label").textContent = ownKey ? modelLabel() : "Set an API key";
  btn.classList.toggle("gx-ownkey-btn-set", !!ownKey);
  btn.classList.toggle("gx-ownkey-btn-unset", !ownKey);
  btn.title = ownKey ? `Questions are answered with your ${PROVIDERS[ownProvider].name} key ${tail(ownKey)} (${modelLabel()}); click to switch or add keys`
    : "No API key set: questions list the matching evidence. Click to add your own key (Anthropic, OpenAI or OpenRouter) and have them answered in words";
  $("search-input").placeholder = ownKey ? "Ask a question..." : "Search by asking...";
}
// Saved keys show which one is in use. The shared add/edit form opens on request, or for the first key.
let ownForm = { editing: "", provider: "" };
function ownKeyMenu(note = "") {
  const box = $("ownkey-menu");
  const editing = ownKeys.find((k) => k.id === ownForm.editing);
  const formOpen = !ownKeys.length || ownForm.open || !!editing;
  const shown = ownForm.provider || editing?.provider || (ownKeys.length ? ownProvider : "anthropic");
  const p = PROVIDERS[shown];
  const sameKey = editing && editing.provider === shown;
  const model = sameKey ? editing.model : freshModel(shown);
  box.innerHTML = `
    ${!ownKeys.length ? `<p class="gx-ownkey-note">Add a key to answer questions in words.</p>` : ""}
    ${note ? `<p class="gx-account-note gx-err" role="alert">${esc(note)}</p>` : ""}
    ${ownKeys.length ? `<div class="gx-ownkey-list" role="radiogroup" aria-label="Saved keys">${ownKeys.map((k) => `
      <div class="gx-ownkey-row${k.id === ownActive ? " is-active" : ""}">
        <label class="gx-ownkey-use" title="${esc(labelOf(k))}">
          <input type="radio" name="ownkey-active" class="gx-ownkey-radio" data-use="${esc(k.id)}"${k.id === ownActive ? " checked" : ""}>
          <span><b>${esc(k.provider === "anthropic" ? labelOf(k) : k.model)}</b><small>${esc(PROVIDERS[k.provider].name)} key ${esc(tail(k.key))}${k.workspace ? ` · workspace ${esc(k.workspace)}` : ""}</small></span></label>
        <button type="button" class="gx-ownkey-act" data-edit="${esc(k.id)}" aria-label="Edit ${esc(PROVIDERS[k.provider].name)} key ${esc(tail(k.key))}" title="Change its model or replace the key">Edit</button>
        <button type="button" class="gx-ownkey-act" data-remove="${esc(k.id)}" aria-label="Remove ${esc(PROVIDERS[k.provider].name)} key ${esc(tail(k.key))}" title="Forget this key">Remove</button>
      </div>`).join("")}</div>` : ""}
    ${formOpen ? `<form class="gx-ownkey-form" id="ownkey-form" aria-label="${editing ? "Edit key" : "Add key"}">
      <div class="gx-ownkey-formhead">${editing ? "Edit key" : "Add key"}${ownKeys.length ? `<button type="button" class="gx-ownkey-act" id="ownkey-cancel">Cancel</button>` : ""}</div>
      <label>Provider<select class="gx-select" id="ownkey-provider">${Object.entries(PROVIDERS).map(([k, v]) =>
        `<option value="${k}"${k === shown ? " selected" : ""}>${esc(v.name)}</option>`).join("")}</select></label>
      <label>API key<input type="password" id="ownkey-input" autocomplete="off" spellcheck="false"
        placeholder="${sameKey ? `${tail(editing.key)} kept; paste another to replace it` : p.hint}"></label>
      <label>Model${shown === "anthropic"
        ? `<select class="gx-select" id="ownkey-model">${OWN_MODELS.map(([m, n]) => `<option value="${m}"${m === model ? " selected" : ""}>${n}</option>`).join("")}</select>`
        : `<input type="text" id="ownkey-model" autocomplete="off" spellcheck="false" value="${esc(model)}" placeholder="${esc(p.model)}">`}</label>
      ${shown === "anthropic" ? `<details class="gx-ownkey-advanced"${sameKey && editing.workspace ? " open" : ""}><summary>Advanced</summary>
        <label>Workspace ID<input type="text" id="ownkey-workspace" autocomplete="off" spellcheck="false" aria-describedby="ownkey-workspace-help"
          placeholder="wrkspc_..." value="${esc(sameKey ? editing.workspace || "" : "")}"></label>
        <small id="ownkey-workspace-help">Only for a key not scoped to a workspace.</small></details>` : ""}
      <button class="gx-btn gx-btn-primary" type="submit">${editing ? "Save changes" : "Save key"}</button>
    </form>` : `<button class="gx-ownkey-act gx-ownkey-add" type="button" id="ownkey-add" aria-expanded="false">+ Add key</button>`}
    <p class="gx-ownkey-note">Stored in this browser, sent only to ${esc(p.host)}.
      <a href="${p.keys}" target="_blank" rel="noopener">Use a key with a spend limit.</a></p>
    <div class="gx-ownkey-footer">
      <label class="gx-ownkey-keep" title="Otherwise, keys are kept until this tab closes"><input type="checkbox" id="ownkey-keep"${ownKeep ? " checked" : ""}>Remember on this device</label>
      ${ownKeys.length ? `<button class="gx-ownkey-act" type="button" id="ownkey-forget" aria-label="Forget all keys">Forget all</button>` : ""}
    </div>`;
  box.querySelectorAll("[data-use]").forEach((b) => (b.onchange = () => {
    ownActive = b.dataset.use; useOwnKeys(); renderOwnKey(); ownKeyMenu(); box.querySelector("[data-use]:checked").focus();
  }));
  box.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => { ownForm = { editing: b.dataset.edit, provider: "" }; ownKeyMenu(); $("ownkey-input").focus(); }));
  box.querySelectorAll("[data-remove]").forEach((b) => (b.onclick = () => {
    ownKeys = ownKeys.filter((k) => k.id !== b.dataset.remove);
    if (ownForm.editing === b.dataset.remove) ownForm = { editing: "", provider: "" };
    useOwnKeys(); renderOwnKey(); ownKeyMenu();
    (box.querySelector("[data-use]:checked") || $("ownkey-input")).focus();
  }));
  $("ownkey-add")?.addEventListener("click", () => { ownForm = { editing: "", provider: "", open: true }; ownKeyMenu(); $("ownkey-input").focus(); });
  $("ownkey-cancel")?.addEventListener("click", () => {
    ownForm = { editing: "", provider: "" }; ownKeyMenu();
    (editing ? [...box.querySelectorAll("[data-edit]")].find((b) => b.dataset.edit === editing.id) : $("ownkey-add")).focus();
  });
  $("ownkey-provider")?.addEventListener("change", (e) => { ownForm = { ...ownForm, provider: e.target.value }; ownKeyMenu(); $("ownkey-provider").focus(); });
  $("ownkey-keep").onchange = (e) => { ownKeep = e.target.checked; useOwnKeys(); };
  $("ownkey-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const key = $("ownkey-input").value.trim() || (sameKey ? editing.key : "");   // a key is never carried to another provider
    if (!key) { ownKeyMenu(`Paste your ${p.name} API key first.`); $("ownkey-input").focus(); return; }
    const m = $("ownkey-model").value.trim();
    if (!goodModel(shown, m)) { ownKeyMenu("Give a model name."); $("ownkey-model").focus(); return; }
    const entry = { id: editing?.id || `k${Date.now().toString(36)}`, provider: shown, key, model: m, workspace: $("ownkey-workspace")?.value.trim() || "" };
    ownKeys = editing ? ownKeys.map((k) => k.id === editing.id ? entry : k) : [...ownKeys.filter((k) => !(k.provider === shown && k.key === key)), entry];
    ownActive = entry.id; ownForm = { editing: "", provider: "" };
    useOwnKeys(); renderOwnKey();
    box.hidden = true; $("ownkey-btn").setAttribute("aria-expanded", "false");
    $("ownkey-btn").focus();
  });
  $("ownkey-forget")?.addEventListener("click", () => {
    ownKeys = []; ownActive = ""; ownForm = { editing: "", provider: "" };
    useOwnKeys(); renderOwnKey(); ownKeyMenu(); $("ownkey-input").focus();
  });
  box.hidden = false;
  $("ownkey-btn").setAttribute("aria-expanded", "true");
}
function openOwnKeyMenu() {
  for (const [, b] of menus) if (b !== $("ownkey-menu")) b.hidden = true;
  ownForm = { editing: "", provider: "" };
  ownKeyMenu();
  ($("ownkey-input") || $("ownkey-menu").querySelector("[data-use]:checked")).focus();
}

// ------------------------------------------------------------------------------------------ example questions
// A graph's example questions (graphs/<id>/questions.yaml, carried by the site build): shown under the empty question
// box, so a reader sees what the graph answers well. A click asks it.
function showExamples() {
  const box = $("examples"), qs = G?.entry?.questions || [];
  if (!qs.length || $("search-input").value.trim() || box.dataset.closed) { box.hidden = true; return; }
  box.innerHTML = `<div class="gx-examples-head">Try a question this graph answers</div>` + qs.map((q, i) =>
    `<button type="button" class="gx-example" role="option" data-i="${i}">${esc(q.question)}${q.kind ? `<span class="gx-example-kind">${esc(q.kind)}</span>` : ""}</button>`).join("")
    + `<div class="gx-examples-note">Best for what the sources report: conditions and outcomes, preparations, what failed and why. A question that chains reasoning across many sources works better asked in parts.</div>`;
  box.querySelectorAll(".gx-example").forEach((b) => (b.onclick = () => {           // a click, Enter or Space asks it
    $("search-input").value = qs[Number(b.dataset.i)].question;
    box.hidden = true;
    $("search-input").focus({ preventScroll: true });
    $("search-form").requestSubmit();
  }));
  box.hidden = false;
}
function exampleKeys(e) {                     // arrows move through the questions, Escape closes them
  const box = $("examples"), opts = [...box.querySelectorAll(".gx-example")];
  if (box.hidden || !opts.length) return;
  const at = opts.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = e.key === "ArrowDown" ? (at + 1) % opts.length : at - 1;      // up from the first returns to the box
    (next < 0 ? $("search-input") : opts[next]).focus();
  } else if (e.key === "Escape") {
    box.hidden = true;
    box.dataset.closed = "1";                 // closed until the reader types or leaves the box
    $("search-input").focus();
  }
}
$("search-input").addEventListener("focus", showExamples);
$("search-input").addEventListener("input", () => { delete $("examples").dataset.closed; showExamples(); });
$("search-form").addEventListener("keydown", exampleKeys);
$("search-form").addEventListener("focusout", (e) => {          // hidden once focus leaves the question box and its list
  if (!$("search-form").contains(e.relatedTarget)) { $("examples").hidden = true; delete $("examples").dataset.closed; }
});
$("examples").addEventListener("mousedown", (e) => e.preventDefault());   // a click keeps the focus in the box

// ------------------------------------------------------------------------------------------ controls
$("search-form").onsubmit = (e) => {
  $("examples").hidden = true;
  e.preventDefault();
  const q = $("search-input").value.trim();
  if (!q || !G) return;
  if (ownKey && G.loaded) askOwnKey(q);                 // a bundle drawn a window at a time is searched, not asked
  else if (SERVICE && token) ask(q); else search(q);
};
$("labels-btn").onclick = () => { S.linkLabels = linkLabelsShown() ? "off" : "on"; draw(); };
$("unhide-btn").onclick = () => { S.hidden.clear(); draw(); };
const menus = [[$("layout-btn"), $("layout-menu")], [$("export-btn"), $("export-menu")], [$("account-btn"), $("account-menu")], [$("ownkey-btn"), $("ownkey-menu")]];
for (const [btn, box] of menus) btn.onclick = (e) => { e.stopPropagation(); for (const [, b] of menus) if (b !== box) b.hidden = true; box.hidden = !box.hidden; btn.setAttribute("aria-expanded", String(!box.hidden)); };
document.addEventListener("click", () => { for (const [, b] of menus) b.hidden = true; });
$("account-btn").onclick = (e) => {
  e.stopPropagation();
  for (const [, b] of menus) if (b !== $("account-menu")) b.hidden = true;
  if ($("account-menu").hidden) accountMenu();
  else { $("account-menu").hidden = true; $("account-btn").setAttribute("aria-expanded", "false"); }
};
$("account-menu").onclick = (e) => e.stopPropagation();
$("ownkey-btn").onclick = (e) => {
  e.stopPropagation();
  if ($("ownkey-menu").hidden) openOwnKeyMenu();
  else { $("ownkey-menu").hidden = true; $("ownkey-btn").setAttribute("aria-expanded", "false"); }
};
$("ownkey-menu").onclick = (e) => e.stopPropagation();
renderOwnKey();
$("layout-menu").querySelectorAll("[data-layout]").forEach((b) => (b.onclick = () => {
  S.layout = b.dataset.layout;
  $("layout-label").textContent = { force: "Force", hierarchical: "Hierarchical", radial: "Radial" }[S.layout];
  $("layout-menu").querySelectorAll("[data-layout]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
  applyLayout(S.layout);
}));
$("zoom-in").onclick = () => S.zoom && svg.transition().duration(200).call(S.zoom.scaleBy, 1.3);
$("zoom-out").onclick = () => S.zoom && svg.transition().duration(200).call(S.zoom.scaleBy, 1 / 1.3);
$("fit").onclick = () => fit();
$("locate").onclick = () => { const n = S.nodes.find((x) => x.id === S.selected); if (n) { S.viewMoved = true; svg.transition().duration(450).call(S.zoom.translateTo, n.x, n.y); } };
$("refresh").onclick = () => { if (!G) return; S.pos.clear(); S.nodes = []; draw(); };
function download(file, blob) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = file; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }
function svgText() {
  const clone = svg.node().cloneNode(true), cs = getComputedStyle(document.documentElement);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.querySelectorAll(".gx-node circle").forEach((c) => { c.setAttribute("stroke", cs.getPropertyValue("--node-stroke")); c.setAttribute("stroke-width", "1.5"); });
  clone.querySelectorAll(".gx-node text").forEach((t) => { t.setAttribute("font-size", "11"); t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", cs.getPropertyValue("--fg")); t.setAttribute("font-family", "Inter, sans-serif"); });
  clone.querySelectorAll("#links path").forEach((l) => { l.setAttribute("stroke-width", "1.6"); l.setAttribute("fill", "none"); });
  return new XMLSerializer().serializeToString(clone);
}
$("export-menu").querySelectorAll("[data-export]").forEach((b) => (b.onclick = () => {
  if (!G) return;
  const kind = b.dataset.export, csv = (rows) => rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
  if (S.big && kind === "svg") { alert("This graph is drawn on a canvas: export it as PNG, or download the graph as JSON."); return; }
  if (S.big && kind === "png") {
    const from = $("gx-canvas"), c = document.createElement("canvas");
    c.width = from.width; c.height = from.height;
    const ctx = c.getContext("2d"); ctx.fillStyle = getComputedStyle(document.body).backgroundColor; ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(from, 0, 0);
    c.toBlob((blob) => download(`${G.entry.id}.png`, blob));
    return;
  }
  if (kind === "svg") download(`${G.entry.id}.svg`, new Blob([svgText()], { type: "image/svg+xml" }));
  if (kind === "nodes-csv") download(`${G.entry.id}-nodes.csv`, new Blob([csv([["id", "label", "name"], ...S.nodes.map((n) => [n.id, n.label, n.name])])], { type: "text/csv" }));
  if (kind === "edges-csv") download(`${G.entry.id}-relationships.csv`, new Blob([csv([["id", "type", "source", "target"], ...S.links.map((e) => [e.id, e.type, e.source.id ?? e.source, e.target.id ?? e.target])])], { type: "text/csv" }));
  if (kind === "graph") download(`${G.entry.id}.json`, new Blob([JSON.stringify(G.doc)], { type: "application/json" }));
  if (kind === "png") {
    const [W, H] = size(), img = new Image(), url = URL.createObjectURL(new Blob([svgText()], { type: "image/svg+xml" }));
    img.onload = () => {
      const c = document.createElement("canvas"); c.width = W * 2; c.height = H * 2;
      const ctx = c.getContext("2d"); ctx.fillStyle = getComputedStyle(document.body).backgroundColor; ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
      c.toBlob((blob) => download(`${G.entry.id}.png`, blob));
    };
    img.src = url;
  }
}));
function panel(which, on) {
  $("grid").classList.toggle(`${which}-off`, !on);
  $(`${which}-show`).hidden = on;
  relayout();
}
// The canvas changed width: the graph re-centres once (not on every frame of a drag).
function relayout() {
  if (S.sim && !S.viewMoved) setTimeout(() => fit(), 50);
}
// The panels' widths are the reader's: a drag on a border (measured from the grid's edge), the arrow keys (Shift for
// bigger steps), double-click or Home for the default. Each is kept between its minimum and half the window, and
// remembered on this device.
function resizer(handle, prop, side, min, def) {
  const grid = $("grid"), key = `scale-explorer-${prop}`;
  const max = () => Math.max(min, Math.round(window.innerWidth * 0.5));
  let width = def;
  try { width = Number(localStorage.getItem(key)) || def; } catch { /* storage off */ }
  const paint = () => { const shown = Math.min(width, max()); grid.style.setProperty(`--${prop}`, `${shown}px`); handle.setAttribute("aria-valuenow", String(shown)); };
  const apply = (px) => { width = Math.round(Math.min(Math.max(px, min), max())); paint(); };
  const keep = () => { try { localStorage.setItem(key, String(width)); } catch { /* storage off */ } relayout(); };
  handle.setAttribute("aria-valuemin", String(min));
  apply(width);
  let dragging = false;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true; handle.dataset.dragging = "true"; document.body.dataset.resizing = "true";
    try { handle.setPointerCapture(e.pointerId); } catch { /* unsupported */ }
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const r = grid.getBoundingClientRect();
    apply(side === "left" ? e.clientX - r.left : r.right - e.clientX);
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false; delete handle.dataset.dragging; delete document.body.dataset.resizing;
    try { handle.releasePointerCapture(e.pointerId); } catch { /* released */ }
    keep();
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  handle.addEventListener("dblclick", () => { apply(def); keep(); });
  handle.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 48 : 16, grow = side === "left" ? 1 : -1;
    let next = null;
    if (e.key === "ArrowLeft") next = width - step * grow;
    else if (e.key === "ArrowRight") next = width + step * grow;
    else if (e.key === "Home") next = def;
    if (next === null) return;
    e.preventDefault(); apply(next); keep();
  });
  window.addEventListener("resize", paint);                 // a narrower window shows less, the chosen width is kept
}
resizer($("rail-resizer"), "rail-w", "left", 200, 280);
resizer($("inspector-resizer"), "inspector-w", "right", 240, 300);
$("rail-collapse").onclick = () => panel("rail", false);
$("rail-show").onclick = () => panel("rail", true);
$("inspector-collapse").onclick = () => panel("inspector", false);
$("inspector-show").onclick = () => panel("inspector", true);
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => { document.documentElement.dataset.theme = e.matches ? "dark" : "light"; });

start().catch((err) => status(`Could not start: ${err.message}`));
