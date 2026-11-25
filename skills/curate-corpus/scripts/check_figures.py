"""Check a scale-catal document's figure values before finishing (references/recipes.md, "Reading a figure panel by
panel"): a caption value copied onto several records, a named figure that neither a value nor a gap cites, and figure
evidence without a transcription.

    python3 -I check_figures.py <document.graph.json> <figure number>[,<figure number>...]

Prints one line per problem and exits 1, or prints "ok: figure values clean" and exits 0. A figure counts as covered
when a value cites it, or when a field left `not_reported` (an unreadable panel) cites it in its evidence or its note.
"""
import collections
import json
import re
import sys

FIG = re.compile(r"\bfig(?:ure)?\.?\s*(\d+)", re.I)


def fig_of(text):
    m = FIG.search(str(text or ""))
    return m[1] if m else None


def check(doc, figs):
    copies = collections.defaultdict(set)      # (figure, value, unit) -> records holding it from a caption
    count = collections.Counter()               # figure -> values citing it
    gaps = set()                                # figures explained by a not_reported field
    bare = []                                   # figure values without a transcription
    for rec in doc.get("nodes", []) + doc.get("links", []):
        props = rec.get("props") or {}
        evd = {e.get("id"): e for e in (props.get("evidence") or []) if isinstance(e, dict)}

        def walk(x):
            if isinstance(x, dict):
                if x.get("status") == "not_reported":
                    cited = [evd[i] for i in x.get("evidence_ids") or [] if i in evd]
                    for f in {fig_of(e.get("locator")) for e in cited} | set(FIG.findall(str(x.get("note") or ""))):
                        if f in figs:
                            gaps.add(f)
                value = x.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    es = [evd[i] for i in x.get("evidence_ids") or [] if i in evd] or list(evd.values())
                    for e in es:
                        f = fig_of(e.get("locator"))
                        if f not in figs:
                            continue
                        count[f] += 1
                        if (re.search(r"caption", str(e.get("locator", "")), re.I) or e.get("kind") == "text"
                                or not x.get("variable")):
                            copies[(f, value, x.get("unit"))].add(rec.get("id"))
                        elif e.get("kind") == "figure" and not (e.get("transcription") or e.get("quote")):
                            bare.append((f, rec.get("id"), value))
                        break
                for k, v in x.items():
                    if k != "evidence":
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(props)
    problems = []
    for (f, v, u), recs in sorted(copies.items(), key=lambda kv: str(kv[0])):
        if len(recs) > 1:
            problems.append(f"Figure {f}: the caption value {v} {u or ''} is copied onto {len(recs)} records; state it "
                            "once, on the record of the series it covers (its Experiment or sample), and remove the copies")
    for f in figs:
        if not count[f] and f not in gaps:
            problems.append(f"Figure {f}: no value cites it yet; record its values, or a gap per unreadable panel "
                            "naming the panel and the reason (a scheme of the preparation has none: its steps go "
                            "with the procedure)")
    for f, r, v in bare[:10]:
        problems.append(f"Figure {f}: value {v} on {r} has figure evidence without a transcription")
    return problems


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    with open(argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
    problems = check(doc, [f.strip() for f in argv[2].split(",") if f.strip()])
    print("\n".join(problems) if problems else "ok: figure values clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
