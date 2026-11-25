#!/usr/bin/env python3
"""An optional passage index of curated sources' page texts, so that a later re-reading reads passages, not papers.

    passage_index.py add --db passages.db <source-folder>...      # index work/texts/*.txt of each folder
    passage_index.py search --db passages.db --source <id> --q anneal --q "calcined at" [--semantic "..."]
    passage_index.py stats --db passages.db

`add` reads a `scale source start` folder's page texts (`work/texts/<slug>.txt`, pages headed `[page N]`, supporting
information `[page S1]` …) and cuts each page into passages of whole paragraphs (about CHUNK characters). A passage
keeps its page and its character span in that page's text, so its text is a verbatim substring of the page the
graph's locators name. Adding a source again replaces its passages (the index follows the page texts). The source id
is the folder's name (the source's slug). `curate_run.py prepare --passage-index <db>` calls `add` for every folder
it starts.

`search` runs each --q as an SQLite FTS5 query (BM25; "phrases" and OR work) and each --pattern as a regular
expression (printed forms words miss: 1:1 w/w, 73/27, 5 wt%) within the given sources and, when the
index has vectors and --semantic is given, ranks the same passages by meaning; the runs are merged by reciprocal rank
fusion. Each hit comes with its neighbours and its locator. A source with no hit is reported as "none found in the
passages": that is not "the source does not state it" (read its pages before saying so).

Vectors are optional: `add --embed-model <dir>` (a sentence-transformers model folder; needs torch and transformers)
stores one per passage. Without them search uses FTS5 only.

The index holds the sources' full page texts: keep it beside the run folders, private, as the page texts are; never
commit or contribute it. It is a finding aid only: every value is still read on the page, quoted verbatim and checked
by `scale graph validate --source-text`. Standard library only (sqlite3 with FTS5), except for vectors.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

CHUNK = 1200          # target passage length, characters
MAX_CHUNK = 2400      # a paragraph longer than this is cut at sentence ends
PAGE = re.compile(r"^\[page (S?\d+)\]\n?", re.M)

SCHEMA = """
create table if not exists meta(key text primary key, value text);
create table if not exists sources(source_id text primary key, text_file text, text_sha256 text, pages int, chars int);
create table if not exists passages(id integer primary key, source_id text, ord int, page text, char_start int,
                                    char_end int, para_first int, para_last int, text text);
create index if not exists passages_source on passages(source_id, ord);
create virtual table if not exists passages_fts using fts5(text, content='passages', content_rowid='id',
                                                           tokenize="porter unicode61");
create table if not exists vectors(id integer primary key, vec blob);
"""


def pages_of(text):
    """[(page, text)] from page texts headed "[page N]" (as `scale source start` writes them); one page otherwise."""
    if not PAGE.search(text):
        return [("1", text.rstrip())]
    parts = PAGE.split(text)[1:]
    return [(parts[i], parts[i + 1].rstrip()) for i in range(0, len(parts), 2)]


def paragraphs(text):
    """(start, end) spans of the page's paragraphs (runs of text between blank lines), long ones cut at sentences."""
    spans, pos = [], 0
    for m in re.finditer(r"\n[ \t]*\n", text):
        if text[pos:m.start()].strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text)))
    out = []
    for s, e in spans:
        while e - s > MAX_CHUNK:
            cut = text.rfind(". ", s + CHUNK // 2, s + MAX_CHUNK)
            cut = cut + 1 if cut > 0 else s + MAX_CHUNK
            out.append((s, cut))
            s = cut
            while s < e and text[s].isspace():
                s += 1
        if s < e:
            out.append((s, e))
    return out


def passages_of(page_text):
    """Whole paragraphs grouped to about CHUNK characters: (start, end, first_para, last_para)."""
    paras = paragraphs(page_text)
    out, i = [], 0
    while i < len(paras):
        s, e, j = paras[i][0], paras[i][1], i
        while j + 1 < len(paras) and paras[j + 1][1] - s <= CHUNK:
            j += 1
            e = paras[j][1]
        out.append((s, e, i + 1, j + 1))
        i = j + 1
    return out


def connect(db_path):
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    return db


class Embedder:
    """Mean-pooled, normalised sentence vectors (float16) from a local sentence-transformers model folder."""

    def __init__(self, model_dir):
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        self.tok = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModel.from_pretrained(model_dir).to(self.dev).eval()

    def __call__(self, texts):
        import numpy as np
        enc = self.tok(texts, padding=True, truncation=True, max_length=256, return_tensors="pt").to(self.dev)
        with self.torch.no_grad():
            h = self.model(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
            v = self.torch.nn.functional.normalize((h * m).sum(1) / m.sum(1).clamp(min=1e-6), dim=-1)
        return v.float().cpu().numpy().astype(np.float16)


def add(db, folder, embed=None):
    """Index one source folder's page texts; returns its counts (or why it was left out)."""
    folder = Path(folder)
    texts = sorted((folder / "work" / "texts").glob("*.txt"))
    if not texts:
        return {"source_id": folder.name, "left_out": "no page texts in work/texts"}
    text = texts[0].read_text(encoding="utf-8", errors="ignore")
    sid = folder.name
    old = [r[0] for r in db.execute("select id from passages where source_id=?", (sid,))]
    if old:                                                     # a source added again: its passages are replaced
        db.executemany("insert into passages_fts(passages_fts, rowid, text) select 'delete', id, text from passages "
                       "where id=?", [(i,) for i in old])
        db.execute("delete from vectors where id in (select id from passages where source_id=?)", (sid,))
        db.execute("delete from passages where source_id=?", (sid,))
    pages = pages_of(text)
    rows, ord_ = [], 0
    for page, body in pages:
        for s, e, pf, pl in passages_of(body):
            ord_ += 1
            rows.append((sid, ord_, page, s, e, pf, pl, body[s:e]))
    for r in rows:
        cur = db.execute("insert into passages(source_id,ord,page,char_start,char_end,para_first,para_last,text) "
                         "values (?,?,?,?,?,?,?,?)", r)
        db.execute("insert into passages_fts(rowid, text) values (?,?)", (cur.lastrowid, r[7]))
    db.execute("insert or replace into sources values (?,?,?,?,?)",
               (sid, texts[0].name, hashlib.sha256(text.encode()).hexdigest(), len(pages), len(text)))
    if embed and rows:
        ids = [r[0] for r in db.execute("select id from passages where source_id=? order by ord", (sid,))]
        for i in range(0, len(ids), 64):
            vecs = embed([r[7] for r in rows[i:i + 64]])
            db.executemany("insert or replace into vectors values (?,?)",
                           [(pid, v.tobytes()) for pid, v in zip(ids[i:i + 64], vecs)])
    db.commit()
    return {"source_id": sid, "pages": len(pages), "passages": len(rows)}


def fts_runs(db, sources, terms, limit):
    runs, qs = [], ",".join("?" * len(sources))
    where = f"and p.source_id in ({qs})" if sources else ""
    sql = (f"select p.id from passages_fts f join passages p on p.id=f.rowid where passages_fts match ? {where} "
           "order by bm25(passages_fts) limit ?")
    for t in terms:
        try:
            runs.append([r[0] for r in db.execute(sql, [t, *sources, limit])])
        except sqlite3.OperationalError:                       # a term FTS5 cannot parse: searched as a phrase
            runs.append([r[0] for r in db.execute(sql, ['"' + t.replace('"', "") + '"', *sources, limit])])
    return runs


def pattern_runs(db, sources, patterns, limit):
    """One run per regular expression: the sources' passages ranked by how often it matches (numbers, units and
    printed forms that words do not find, e.g. 1:1 w/w, 73/27, 5 wt%)."""
    if not patterns:
        return []
    qs = ",".join("?" * len(sources))
    rows = list(db.execute(f"select id, text from passages {'where source_id in (' + qs + ')' if sources else ''}",
                           sources))
    runs = []
    for pat in patterns:
        rx = re.compile(pat, re.I)
        scored = sorted(((len(rx.findall(t)), i) for i, t in rows), reverse=True)
        runs.append([i for n, i in scored[:limit] if n])
    return runs


def semantic_run(db, db_path, sources, sentence, limit, model_dir=None):
    """The sources' passages by cosine to the sentence: vectors in the index, or a .npy the index's meta names."""
    import numpy as np
    meta = dict(db.execute("select key, value from meta"))
    model_dir = model_dir or meta.get("embed_model")
    if not model_dir:
        return None
    qs = ",".join("?" * len(sources))
    ids = [r[0] for r in db.execute(f"select id from passages where source_id in ({qs})", sources)]
    if not ids:
        return []
    if meta.get("embed_file"):                                 # one row per passage id (row = id - 1)
        mat = np.load(Path(db_path).parent / meta["embed_file"], mmap_mode="r")[[i - 1 for i in ids]]
    else:
        got = dict(db.execute(f"select id, vec from vectors where id in ({','.join('?' * len(ids))})", ids))
        ids = [i for i in ids if i in got]
        if not ids:
            return None
        mat = np.stack([np.frombuffer(got[i], dtype=np.float16) for i in ids])
    q = Embedder(model_dir)([sentence])[0].astype("float32")
    scores = mat.astype("float32") @ q
    return [ids[i] for i in scores.argsort()[::-1][:limit]]


def rrf(runs, k=60):
    score = {}
    for run in runs:
        for rank, pid in enumerate(run):
            score[pid] = score.get(pid, 0) + 1 / (k + rank + 1)
    return sorted(score, key=score.get, reverse=True)


COLS = ("id", "source_id", "ord", "page", "char_start", "char_end", "para_first", "para_last", "text")


def search(db, db_path, sources, terms, semantic=None, k=10, neighbors=1, model_dir=None, patterns=()):
    runs = fts_runs(db, sources, terms, k * 3) + pattern_runs(db, sources, list(patterns), k * 3)
    note = None
    if semantic and sources:
        sem = semantic_run(db, db_path, sources, semantic, k * 3, model_dir)
        if sem is None:
            note = "no vectors in this index: FTS5 only"
        else:
            runs.append(sem)
    hits = []
    for pid in rrf(runs)[:k]:
        h = dict(zip(COLS, db.execute(f"select {','.join(COLS)} from passages where id=?", (pid,)).fetchone()))
        h["neighbors"] = [dict(zip(("ord", "page", "text"), r)) for r in db.execute(
            "select ord, page, text from passages where source_id=? and ord between ? and ? and ord<>? order by ord",
            (h["source_id"], h["ord"] - neighbors, h["ord"] + neighbors, h["ord"]))]
        hits.append(h)
    found = {h["source_id"] for h in hits}
    out = {"hits": hits, "none_found_in_passages": [s for s in sources if s not in found]}
    if note:
        out["note"] = note
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="index the page texts of source folders")
    p.add_argument("--db", required=True)
    p.add_argument("--embed-model", help="a sentence-transformers model folder: also store vectors (torch)")
    p.add_argument("folders", nargs="+")
    p = sub.add_parser("search", help="passages of given sources that may hold what is asked")
    p.add_argument("--db", required=True)
    p.add_argument("--source", action="append", default=[], help="a source id, the folder's name (repeatable)")
    p.add_argument("--q", action="append", default=[], help="an FTS5 term or phrase (repeatable)")
    p.add_argument("--pattern", action="append", default=[], help="a regular expression (repeatable): printed "
                   "forms words miss, e.g. '\\d+\\s*:\\s*\\d+' or 'w\\s*/\\s*w'")
    p.add_argument("--semantic", help="a sentence to rank the sources' passages by meaning (needs vectors)")
    p.add_argument("--embed-model", help="the model the vectors were made with (default: the index's meta)")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--neighbors", type=int, default=1)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("stats")
    p.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    db = connect(a.db)
    if a.cmd == "add":
        embed = Embedder(a.embed_model) if a.embed_model else None
        if a.embed_model:
            db.execute("insert or replace into meta values ('embed_model', ?)", (str(Path(a.embed_model).resolve()),))
        for f in a.folders:
            print(json.dumps(add(db, f, embed)))
    elif a.cmd == "search":
        if not a.q and not a.semantic and not a.pattern:
            ap.error("give at least one --q, --pattern or --semantic")
        out = search(db, a.db, a.source, a.q, a.semantic, a.k, a.neighbors, a.embed_model, a.pattern)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            for h in out["hits"]:
                print(f"== {h['source_id']}  page {h['page']}  chars {h['char_start']}-{h['char_end']}  "
                      f"paragraphs {h['para_first']}-{h['para_last']}\n{h['text']}\n")
            for s in out["none_found_in_passages"]:
                print(f"-- {s}: none found in the passages (not 'the source does not state it')")
            if out.get("note"):
                print(f"-- {out['note']}")
    else:
        n = lambda q: db.execute(q).fetchone()[0]
        print(json.dumps({"sources": n("select count(*) from sources"), "passages": n("select count(*) from passages"),
                          "vectors": n("select count(*) from vectors"), **dict(db.execute("select * from meta"))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
