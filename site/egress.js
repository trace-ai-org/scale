// Egress in the graph explorer: what a graph's records may be sent to (docs/private-data.md). A graph opened from a
// file may hold records that are not public; the page then sends nothing drawn from it (tool results, the graph
// itself) to a model provider or to the service unless the reader has approved that destination for those access
// levels in this page. The approval lives in this page only (never stored), so a reload denies again. The levels are
// read as SCALE's own rule reads them (pgraph.openness): a missing or malformed access is institution, an embargo in
// force institution, a lapsed one its access_after_embargo.
export const LEVELS = ["public", "consortium", "project", "institution"];

const DATE = /^\d{4}(-\d{2}(-\d{2})?)?$/;
function embargoEnd(until) {                     // the last day an embargo holds, or null (never lapses: fails closed)
  if (typeof until !== "string" || !DATE.test(until)) return null;
  const [y, m, d] = until.split("-").map(Number);
  if (m === undefined) return `${until}-12-31`;
  if (m < 1 || m > 12) return null;
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  if (d === undefined) return `${until}-${String(last).padStart(2, "0")}`;
  return d >= 1 && d <= last ? until : null;
}
const stewardshipOf = (v) => (v === null || v === undefined ? {} : typeof v === "object" && !Array.isArray(v) ? v : { access: "institution" });
function effective(graphSt, rec) {
  const st = { ...graphSt };
  const props = rec && rec.props;
  const own = props && typeof props === "object" && !Array.isArray(props) ? stewardshipOf(props.stewardship) : stewardshipOf(props || null);
  if (own.access && own.access !== "embargoed") { delete st.embargo_until; delete st.access_after_embargo; }
  return Object.assign(st, own);
}
export function openness(st, today) {
  let access = st.access || "institution";
  if (access === "embargoed") {
    const end = embargoEnd(st.embargo_until);
    access = end !== null && today > end ? (st.access_after_embargo || "institution") : "institution";
  }
  return LEVELS.includes(access) ? access : "institution";
}
export const todayISO = () => new Date().toISOString().slice(0, 10);
// The access levels a document's header and records grant today that are not public, least open last ([] when every
// one is public: the graph may go anywhere).
export function closedLevels(doc, today = todayISO()) {
  if (!doc || typeof doc !== "object") return ["institution"];
  const graph = doc.graph && typeof doc.graph === "object" ? doc.graph : {};
  const head = stewardshipOf(graph.stewardship), seen = new Set([openness(head, today)]);
  for (const r of [...(doc.nodes || []), ...(doc.links || [])]) seen.add(r && typeof r === "object" ? openness(effective(head, r), today) : "institution");
  return LEVELS.filter((l) => l !== "public" && seen.has(l));
}
// The levels a destination has not been approved for (approvals: a Set of "<destination>|<level>").
export function denied(levels, destination, approvals) {
  return levels.filter((l) => !approvals.has(`${destination}|${l}`));
}
export function approve(levels, destination, approvals) {
  for (const l of levels) approvals.add(`${destination}|${l}`);
}
export class EgressDenied extends Error {
  constructor(destinationName, levels) {
    super(`Not sent: this graph holds records whose access is ${levels.join(", ")}, and ${destinationName} is not `
      + `approved for ${levels.length > 1 ? "those levels" : "that level"} in this page. Nothing drawn from the graph `
      + `left this browser. Approve it only if the data's project allows ${destinationName} to read such records.`);
    this.name = "EgressDenied";
    this.levels = levels;
  }
}
// A gate to call before every request that would carry something drawn from the graph: it throws EgressDenied while
// any of the graph's closed levels is not approved for the destination.
export function gate(levels, destination, destinationName, approvals) {
  return () => {
    const d = denied(levels, destination, approvals);
    if (d.length) throw new EgressDenied(destinationName, d);
  };
}
