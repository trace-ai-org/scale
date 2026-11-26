// The Analyze view's charts (docs/analyze.md). The data of a chart comes from SCALE's own code in the worker
// (scale.plotdata: plot_data for the graph, table_data for a linked data file); this module turns one panel of it into
// a Vega-Lite spec and draws it. Nothing here reads, converts or groups a value: a panel is one axis typing (one unit,
// one kind, one set of contexts), as the Python side made it. A value is a filled circle (its uncertainty or confidence
// interval as an error bar), a one-sided bound an open triangle pointing the way the value lies (up: at least; down: at
// most; right and left on x), a range a line from its lower to its upper end (a box when both axes are ranges). A line
// joins only a series' values (never a bound or a range) in x order; bars are never stacked or summed.
// Vega, Vega-Lite and vega-embed load only when a chart is first drawn, pinned and checked against their digests, with
// a loader that fetches nothing: every chart's data is inline.
export const VEGA = {
  vega: ["https://cdnjs.cloudflare.com/ajax/libs/vega/5.30.0/vega.min.js", "sha384-em7CHpJd+SsMugVFf6TY7AKQcLWMcbPhD84hmNK8o6WFDkK+2uHSUQRVQV1/w827"],
  "vega-lite": ["https://cdnjs.cloudflare.com/ajax/libs/vega-lite/5.21.0/vega-lite.min.js", "sha384-GhkD6ks9/zgY1m5EFOUZWz/vMVMUFF/92DL61RZc+B42J8osL+jNufKv68bNHHZ2"],
  "vega-embed": ["https://cdnjs.cloudflare.com/ajax/libs/vega-embed/6.26.0/vega-embed.min.js", "sha384-TqXb8su49m5OnEpKGO8m+VrgHesrUxyP22HgpXi4hnh1Hm43dXroiSYemNf5D8lv"],
};
const NUMERIC = new Set(["value", "bound", "range", "number"]);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// One axis value as the reader sees it: "≥ 20 %", "22–26 %", "26 ± 2 %", "Cu".
export function shown(v) {
  if (!v) return "";
  const u = v.unit ? ` ${v.unit}` : "";
  if (v.type === "category") return String(v.value);
  if (v.type === "bound") return `${v.bound === "lower" ? "≥" : "≤"} ${v.value}${u}`;
  if (v.type === "range") return `${v.lower}–${v.upper}${u}`;
  const unc = num(v.uncertainty) !== null ? ` ± ${v.uncertainty}` : "";
  return `${v.value}${unc}${u}${v.qualifier ? ` (${v.qualifier})` : ""}`;
}

// Where a value sits on its axis: {at, to} (a range from at to to), {lo, hi} (its error bar), its marker.
function place(v) {
  if (!v) return {};
  if (v.type === "category") return { at: String(v.value) };
  if (v.type === "range") return { at: v.lower, to: v.upper };
  const out = { at: v.value };
  if (v.type === "value") {
    if (num(v.interval_lower) !== null && num(v.interval_upper) !== null) Object.assign(out, { lo: v.interval_lower, hi: v.interval_upper });
    else if (num(v.uncertainty) !== null) Object.assign(out, { lo: v.value - v.uncertainty, hi: v.value + v.uncertainty });
  }
  return out;
}

// The rows a panel's chart draws: one per point, each with its record (to select it in the graph) and what it shows.
export function rowsOf(panel) {
  return (panel.points || []).map((q) => {
    const x = place(q.x), y = place(q.y);
    const kind = q.y.type === "bound" || q.x.type === "bound" ? "bound"
      : q.y.type === "range" || q.x.type === "range" ? "range" : "value";
    const xm = q.x.type === "bound" ? (q.x.bound === "lower" ? "triangle-right" : "triangle-left") : null;
    const marker = q.y.type === "bound" ? (q.y.bound === "lower" ? "triangle-up" : "triangle-down") : xm || "circle";
    const unc = q.y.uncertainty_kind || (q.y.interval_lower !== undefined ? `interval${q.y.confidence_level ? ` (${q.y.confidence_level})` : ""}` : "");
    const where = [...new Set((q.evidence || []).map((e) => [e.source_name, e.source_year, e.locator].filter(Boolean).join(", ")))];
    return {
      id: q.id, record: q.record || null, row: q.row ?? null, name: q.name || q.id, series: q.series || "", facet: q.facet || "",
      kind, marker, xmarker: q.y.type === "bound" && xm ? xm : null, unc, x: x.at, x2: x.to ?? null, xlo: x.lo ?? null, xhi: x.hi ?? null, y: y.at, y2: y.to ?? null, ylo: y.lo ?? null, yhi: y.hi ?? null,
      xr: q.x.type === "range", yr: q.y.type === "range", xs: shown(q.x), ys: shown(q.y),
      cited: (q.cited_by || []).length ? q.cited_by.map((c) => `${c.record}${c.firm ? "" : " (tentative)"}`).join("; ") : "",
      citedBy: (q.cited_by || [])[0]?.record || null, citedFirm: (q.cited_by || []).some((c) => c.firm),
      conditions: Object.entries(q.conditions || {}).filter(([k]) => k !== "source").map(([k, v]) => `${k}: ${v}`).join(" · "),
      source: (q.conditions || {}).source || "", assay: q.assay || "", target: q.target || "",
      evidence: where.slice(0, 4).join(" | "),
    };
  });
}

// A count: the records per x value (a bar per category or bare number, such as a year), or a histogram of a quantity's
// values; bounds and ranges are never binned (scale.plotdata says how many were not).
// The bins and counts are scale.plotdata's (panel.histogram, panel.buckets), so that the chart, its CSV and an agent read
// the same numbers.
function countLayers(panel, series) {
  const yTitle = panel.y.title || "records";
  if (panel.x.type === "quantity") {
    const base = new Map(), bins = [];
    for (const b of (panel.histogram && panel.histogram.bins) || []) {
      const at = base.get(b.lo) || 0;
      bins.push({ ...b, series: b.series || "", y0: at, y1: at + b.records });
      base.set(b.lo, at + b.records);
    }
    return [{ data: { values: bins }, mark: { type: "rect", opacity: 0.85, stroke: "#ffffff", strokeWidth: 0.5 },
      encoding: { x: { field: "lo", type: "quantitative", title: panel.x.title, scale: { zero: false } }, x2: { field: "hi" },
        y: { field: "y1", type: "quantitative", title: yTitle }, y2: { field: "y0" },
        ...(series ? { color: { field: "series", type: "nominal", title: "Series", legend: { orient: "bottom", columns: 3, labelLimit: 220 } } } : {}),
        tooltip: [{ field: "lo", title: "from" }, { field: "hi", title: "to (excluded)" }, { field: "records", title: "records" },
          ...(series ? [{ field: "series", title: "Series" }] : [])] } }];
  }
  const rows = (panel.buckets || []).map((b) => ({ x: b.x, records: b.records, sources: b.sources, series: b.series || "" }));
  return [{ data: { values: rows }, mark: { type: "bar", opacity: 0.85, ...(panel.x.type === "number" ? { width: { band: 0.9 }, size: 6 } : {}) },
    encoding: { x: panel.x.type === "number" ? { field: "x", type: "quantitative", title: panel.x.title, scale: { zero: false }, axis: { format: "d" } }
      : { field: "x", type: "nominal", title: panel.x.title, sort: "ascending", axis: CATEGORY_AXIS },
      y: { field: "records", type: "quantitative", title: yTitle, stack: "zero" },
      ...(series ? { color: { field: "series", type: "nominal", title: "Series", legend: { orient: "bottom", columns: 3, labelLimit: 220 } } } : {}),
      tooltip: [{ field: "x", title: panel.x.label || "x" }, { field: "records", title: "records" }, { field: "sources", title: "sources" },
        ...(series ? [{ field: "series", title: "Series" }] : [])] } }];
}
// A box per category from scale.plotdata's statistics (panel.boxes: quartiles by linear interpolation, whiskers from the
// minimum to the maximum reported value); bounds and ranges are not in a box and stay markers.
function boxLayers(panel, color) {
  const rows = (panel.boxes || []).filter((b) => b.n > 0).map((b) => ({ ...b, x: b.x, series: b.series || "" }));
  const X = { field: "x", type: panel.x.type === "number" ? "ordinal" : "nominal", title: panel.x.title, sort: "ascending", axis: CATEGORY_AXIS };
  const tip = [{ field: "x", title: panel.x.label || "x" }, { field: "n", title: "values in the box" }, { field: "min" }, { field: "q1" }, { field: "median" },
    { field: "q3" }, { field: "max" }, { field: "n_bounds", title: "bounds (not in the box)" }, { field: "n_ranges", title: "ranges (not in the box)" }, { field: "sources" }];
  const Y = (f) => ({ field: f, type: "quantitative", title: panel.y.title, scale: { zero: false } });
  return [
    { data: { values: rows }, mark: { type: "rule" }, encoding: { x: X, y: Y("min"), y2: { field: "max" }, tooltip: tip } },
    { data: { values: rows }, mark: { type: "bar", size: 18, opacity: 0.55 }, encoding: { x: X, y: Y("q1"), y2: { field: "q3" }, ...color,
      ...(color.color ? { xOffset: { field: "series" } } : {}), tooltip: tip } },
    { data: { values: rows }, mark: { type: "tick", size: 18, thickness: 2, color: "#111" }, encoding: { x: X, y: Y("median"), tooltip: tip } },
  ];
}

// A category axis: labels angled and cut at 18 characters, the whole name in a tooltip on the label.
const CATEGORY_AXIS = { labelAngle: -35, labelAlign: "right", labelLimit: 130,
  labelExpr: "length(datum.label) > 18 ? slice(datum.label, 0, 17) + '…' : datum.label",
  encode: { labels: { interactive: true, update: { tooltip: { signal: "datum.value" } } } } };

// The Vega-Lite spec of one panel. opts: {mark, colors: {fg, muted, line, bg}, width, height}.
export function vegaSpec(panel, opts = {}) {
  const c = { fg: "#0d0d0d", muted: "#6b6b72", line: "#e5e5e7", bg: "#ffffff", ...(opts.colors || {}) };
  const qx = NUMERIC.has(panel.x.type === "quantity" ? "value" : panel.x.type), qy = NUMERIC.has(panel.y.type === "quantity" ? "value" : panel.y.type);
  const mark = opts.mark || panel.mark || "point";
  const X = (field = "x") => ({ field, type: qx ? "quantitative" : "nominal", title: panel.x.title, ...(qx ? { scale: { zero: false } } : { sort: "ascending", axis: CATEGORY_AXIS }) });
  const Y = (field = "y") => ({ field, type: qy ? "quantitative" : "nominal", title: panel.y.title, ...(qy ? { scale: { zero: false } } : {}) });
  const series = (panel.series || []).length > 0;
  const color = series ? { color: { field: "series", type: "nominal", title: "Series", legend: { orient: "bottom", columns: 3, labelLimit: 220 } } } : {};
  const tooltip = [{ field: "name", title: "Record" }, { field: "ys", title: panel.y.label || "y" }, { field: "unc", title: "uncertainty" }, { field: "xs", title: panel.x.label || "x" },
    ...(series ? [{ field: "series", title: "Series" }] : []), { field: "target", title: "product" }, { field: "assay", title: "assay" },
    { field: "conditions", title: "conditions" }, { field: "source", title: "source" }, { field: "evidence", title: "evidence" }, { field: "cited", title: "cited by" }];
  const layer = [
    // error bars: a value's reported uncertainty or confidence interval
    { transform: [{ filter: "datum.ylo !== null && datum.yhi !== null" }], mark: { type: "rule", strokeWidth: 1.2, opacity: 0.8 },
      encoding: { x: X(), y: Y("ylo"), y2: { field: "yhi" }, ...color } },
    { transform: [{ filter: "datum.xlo !== null && datum.xhi !== null" }], mark: { type: "rule", strokeWidth: 1.2, opacity: 0.8 },
      encoding: { y: Y(), x: X("xlo"), x2: { field: "xhi" }, ...color } },
    // ranges: from lower to upper, never a midpoint; a box when both axes are ranges
    { transform: [{ filter: "datum.yr && !datum.xr" }], mark: { type: "rule", strokeWidth: 3, opacity: 0.75, strokeCap: "round" },
      encoding: { x: X(), y: Y(), y2: { field: "y2" }, ...color, tooltip } },
    { transform: [{ filter: "datum.xr && !datum.yr" }], mark: { type: "rule", strokeWidth: 3, opacity: 0.75, strokeCap: "round" },
      encoding: { y: Y(), x: X(), x2: { field: "x2" }, ...color, tooltip } },
    { transform: [{ filter: "datum.xr && datum.yr" }], mark: { type: "rect", fillOpacity: 0.15, strokeWidth: 1.5 },
      encoding: { x: X(), x2: { field: "x2" }, y: Y(), y2: { field: "y2" }, ...color, ...(series ? { stroke: { field: "series", type: "nominal", legend: null } } : {}), tooltip } },
  ];
  if (mark === "line" && qx) {          // a series' values in x order; bounds and ranges are never joined
    layer.push({ transform: [{ filter: "datum.kind === 'value'" }], mark: { type: "line", strokeWidth: 1.2, opacity: 0.6 },
      encoding: { x: X(), y: Y(), ...color, ...(series ? { detail: { field: "series" } } : {}), order: { field: "x", type: "quantitative" } } });
  }
  if (mark === "bar" && !qx && qy) {    // one bar per point, side by side; never stacked, never summed
    layer.push({ transform: [{ filter: "datum.kind === 'value'" }], mark: { type: "bar", opacity: 0.85 },
      encoding: { x: X(), y: { ...Y(), stack: null, scale: { zero: true } }, ...color, xOffset: { field: "id" }, tooltip } });
  } else {
    layer.push({ transform: [{ filter: "datum.kind === 'value'" }], mark: { type: "point", filled: true, size: 55, opacity: 0.9 },
      encoding: { x: X(), y: Y(), ...color, tooltip } });
  }
  layer.push(
    // bounds: an open triangle at the bound, pointing the way the value lies
    { transform: [{ filter: "datum.kind === 'bound'" }], mark: { type: "point", filled: false, size: 110, strokeWidth: 1.8 },
      encoding: { x: X(), y: Y(), shape: { field: "marker", type: "nominal", scale: { domain: ["triangle-up", "triangle-down", "triangle-right", "triangle-left"],
        range: ["triangle-up", "triangle-down", "triangle-right", "triangle-left"] }, legend: null }, ...color, tooltip } },
    // a point bounded on both axes: its x direction too
    { transform: [{ filter: "datum.xmarker !== null" }], mark: { type: "point", filled: false, size: 70, strokeWidth: 1.4 },
      encoding: { x: X(), y: Y(), shape: { field: "xmarker", type: "nominal", scale: { domain: ["triangle-right", "triangle-left"],
        range: ["triangle-right", "triangle-left"] }, legend: null }, ...color, tooltip } },
    // the rows a graph record cites (a linked data file): ringed, solid when a dataset_cell names it, dashed when a locator does
    { transform: [{ filter: "datum.cited !== '' && datum.citedFirm" }], mark: { type: "point", filled: false, size: 260, strokeWidth: 2, color: "#ffae3a" },
      encoding: { x: X(), y: Y(), tooltip } },
    { transform: [{ filter: "datum.cited !== '' && !datum.citedFirm" }], mark: { type: "point", filled: false, size: 260, strokeWidth: 2, color: "#ffae3a", strokeDash: [3, 3] },
      encoding: { x: X(), y: Y(), tooltip } },
  );
  if (panel.y.type === "count") layer.splice(0, layer.length, ...countLayers(panel, series));
  else if (mark === "box" && panel.boxes) {  // a box per category over its values; bounds and ranges stay markers on top
    const boxX = { field: "x", type: panel.x.type === "number" ? "ordinal" : "nominal", title: panel.x.title, sort: "ascending", axis: CATEGORY_AXIS };
    for (const l of layer) if (l.encoding && l.encoding.x && l.encoding.x.field === "x") l.encoding.x = { ...boxX };
    layer.splice(layer.length - 5, 1, ...boxLayers(panel, color),
      { transform: [{ filter: "datum.kind === 'value'" }], mark: { type: "point", filled: true, size: 14, opacity: 0.45 }, encoding: { x: boxX, y: Y(), tooltip } });
  }
  const inner = { layer, width: opts.width ?? "container", height: opts.height ?? 240 };
  const faceted = (panel.facets || []).length > 1 && panel.y.type !== "count" && !(mark === "box" && panel.boxes);
  const spec = {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: { values: rowsOf(panel) },
    ...(faceted ? { facet: { field: "facet", type: "nominal", title: null }, columns: 2, spec: { ...inner, width: opts.facetWidth ?? 260 } } : inner),
    autosize: { type: "fit-x", contains: "padding" },
    background: c.bg,
    config: { view: { stroke: c.line }, axis: { labelColor: c.muted, titleColor: c.fg, gridColor: c.line, domainColor: c.line, tickColor: c.line, titleFontWeight: 500, labelLimit: 160 },
      legend: { labelColor: c.fg, titleColor: c.fg }, header: { labelColor: c.fg }, font: "Inter, system-ui, sans-serif" },
  };
  return spec;
}

// A panel whose aggregates (counts, bins, boxes) are per facet, as one panel per facet: each chart then draws only its
// facet's numbers, never another's.
export function splitFacets(panel) {
  const facets = panel.facets || [];
  if (facets.length < 2 || !(panel.y.type === "count" || (panel.mark === "box" && panel.boxes))) return [panel];
  const of = (xs, f) => (xs || []).filter((b) => (b.facet || "") === f);
  return facets.map((f) => ({ ...panel, title: `${panel.title} · ${f}`, facets: [f], points: of(panel.points, f),
    ...(panel.buckets ? { buckets: of(panel.buckets, f) } : {}), ...(panel.boxes ? { boxes: of(panel.boxes, f) } : {}),
    ...(panel.histogram ? { histogram: { ...panel.histogram, bins: of(panel.histogram.bins, f) } } : {}) }));
}

// The digest of a file's bytes (hex), and how it compares with the digest a record states.
export async function sha256Hex(buffer, subtle = globalThis.crypto?.subtle) {
  const h = await subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
export function digestCheck(recorded, actual) {
  if (typeof recorded !== "string" || !recorded.trim()) return "not_recorded";
  return recorded.trim().toLowerCase().replace(/^sha256:/, "") === actual ? "verified" : "mismatch";
}

// The header of a CSV text (RFC 4180: its first record, quoted fields and doubled quotes read), for the builder's
// column choices; the rows are read by scale.plotdata.table_data in the worker.
export function csvHeader(text) {
  const s = String(text || "").replace(/^﻿/, "");
  const out = [];
  let field = "", quoted = false, i = 0;
  for (; i < s.length; i++) {
    const ch = s[i];
    if (quoted) {
      if (ch === '"' && s[i + 1] === '"') { field += '"'; i++; } else if (ch === '"') quoted = false; else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { out.push(field.trim()); field = ""; } else if (ch === "\n" || ch === "\r") break;
    else field += ch;
  }
  out.push(field.trim());
  return out;
}

// A plot spec from the builder's choices: a value "p:<path>" (a property path), "e:<prop>" (one result entry per point)
// or "c:<catalyst|sample|source>" (a linked record, a category).
// A filter's target from a builder value: "p:<path>", "c:<link>" (a linked record, by name) or "v:<{via, path}>".
export function targetOf(v) {
  return v.startsWith("v:") ? JSON.parse(v.slice(2)) : { path: v.slice(2) };
}

export function specFrom({ label, x, y, series, facet, mark, title, filters }) {
  const axis = (v) => (v === "count" ? "count" : v.startsWith("e:") ? { entries: v.slice(2) } : v.startsWith("v:") ? JSON.parse(v.slice(2)) : v.slice(2));
  const group = (v) => (!v ? undefined : v.startsWith("p:") || v.startsWith("c:") ? v.slice(2) : v);
  const spec = { plot: 1, label, x: axis(x), y: axis(y), mark: mark || "point" };
  if (group(series)) spec.series = group(series);
  if (group(facet)) spec.facet = group(facet);
  if (filters && filters.length) spec.filters = filters;
  if (title) spec.title = title;
  return spec;
}

// The builder's choices for a label, from the graph's stats: the numeric paths (units in use), the result-entry lists
// (a path ending in .*), the linked records and the properties that are text (for series and facets).
export function choices(stats, label) {
  const units = (stats.units_in_use || {})[label] || {};
  const props = (stats.properties_by_label || {})[label] || {};
  const links = (stats.link_patterns || []).filter((l) => l.source === label);
  const numeric = [], entries = [];
  for (const path of Object.keys(units).sort()) {
    if (/^\w+\.\*$/.test(path)) entries.push(path.slice(0, -2));
    else if (!path.includes("*") && !path.includes("[")) numeric.push(path);
  }
  const linked = [["catalyst", "USES_CATALYST"], ["sample", "USES_CATALYST"], ["source", "REPORTED_IN"]]
    .filter(([, t]) => links.some((l) => l.type === t)).map(([n]) => n);
  const text = Object.keys(props).filter((p) => !units[p] && !SKIP.includes(p)
    && !Object.keys(units).some((u) => u.startsWith(`${p}.`))).sort();
  return { numeric, entries, linked, text, via: viaChoices(stats, label) };
}
const SKIP = ["evidence", "name", "note", "description", "stewardship", "origin", "statement", "quote", "title"];
// Values of linked records, one or two links away (a test's sample's BET area, its catalyst's composition, its source's
// year): [{value: "v:<axis json>", text}], each an axis {via: [{edge, to, direction}], path}; links followed either way.
export function viaChoices(stats, label, max = 400) {
  const pats = stats.link_patterns || [], props = stats.properties_by_label || {}, units = stats.units_in_use || {};
  const hops = (from) => [
    ...pats.filter((l) => l.source === from && l.type !== "REPORTED_IN" || l.source === from && l.target === "Source").map((l) => ({ edge: l.type, to: l.target })),
    ...pats.filter((l) => l.target === from && l.source !== from && !["REPORTED_IN", "SUPPORTS", "CONTRADICTS"].includes(l.type)).map((l) => ({ edge: l.type, to: l.source, direction: "in" })),
  ];
  const fields = (lab) => {
    const u = units[lab] || {};
    const ents = Object.keys(u).filter((p) => /^\w+\.\*$/.test(p)).map((p) => p.slice(0, -2));
    const num = Object.keys(u).filter((p) => !p.includes("*") && !p.includes("[")).map((p) => [p, `${p.replace(/_/g, " ")} (${Object.keys(u[p]).slice(0, 2).join(", ")})`]);
    const txt = Object.keys(props[lab] || {}).filter((p) => !u[p] && !SKIP.includes(p) && !Object.keys(u).some((x) => x.startsWith(`${p}.`))).map((p) => [p, p.replace(/_/g, " ")]);
    return [["", `the ${lab} itself`], ...num, ...txt, ...ents.map((e) => [{ entries: e }, `${e.replace(/_/g, " ")} (one result entry per point)`])];
  };
  const out = [], seen = new Set();
  const add = (via, path, text) => {
    const key = JSON.stringify(typeof path === "object" ? { via, ...path } : { via, path });
    if (seen.has(key) || out.length >= max) return;
    seen.add(key);
    out.push({ value: `v:${key}`, text });
  };
  const arrow = (h) => `${h.direction === "in" ? "←" : "→"} ${h.to}`;
  for (const h1 of hops(label)) {
    for (const [p, t] of fields(h1.to)) add([h1], p, `${arrow(h1)}: ${t}`);
    if (h1.to === "Source") continue;
    for (const h2 of hops(h1.to)) {
      if (h2.to === label) continue;
      for (const [p, t] of fields(h2.to)) add([h1, h2], p, `${arrow(h1)} ${arrow(h2)}: ${t}`);
    }
  }
  return out;
}

let loading = null;
// Vega, Vega-Lite and vega-embed, each pinned and checked against its digest (doc: the page's document).
export function loadVega(doc = document) {
  loading ||= ["vega", "vega-lite", "vega-embed"].reduce((p, name) => p.then(() => new Promise((resolve, reject) => {
    const [src, integrity] = VEGA[name];
    const el = Object.assign(doc.createElement("script"), { src, integrity, crossOrigin: "anonymous" });
    el.onload = resolve;
    el.onerror = () => reject(new Error(`${name} could not be loaded`));
    doc.head.append(el);
  })), Promise.resolve()).then(() => globalThis.vegaEmbed);
  loading.catch(() => { loading = null; });
  return loading;
}
// A loader that loads nothing: a chart's data is inline, and nothing it draws may reach another address.
const NO_LOADER = {
  load: () => Promise.reject(new Error("charts here load no data from elsewhere")),
  sanitize: () => Promise.reject(new Error("charts here load nothing from elsewhere")),
  http: () => Promise.reject(new Error("no network")), file: () => Promise.reject(new Error("no files")),
};
// Draw one panel into el; onPick(row) when a point is clicked. Returns the Vega view (for SVG and PNG downloads).
export async function draw(el, panel, opts = {}, onPick = null) {
  const embed = await loadVega();
  const { view } = await embed(el, vegaSpec(panel, opts), { actions: false, renderer: "svg", loader: NO_LOADER, tooltip: true });
  el.setAttribute("role", "img");                    // the points are also listed in a table under the chart
  el.setAttribute("aria-label", panel.title || "chart");
  if (onPick) view.addEventListener("click", (_, item) => {
    const d = item && item.datum;
    if (d && d.id) onPick(d);
    else if (d && d.x !== undefined && panel.y.type === "count") onPick({ bucket: d.x, series: d.series || null });   // a bar of records
    else if (d && d.lo !== undefined && panel.y.type === "count") onPick({ bin: [d.lo, d.hi] });                     // a histogram's bin
  });
  return view;
}
