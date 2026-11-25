"""The data guard: git hooks that keep private data out of a repository whose readers may not read it
(docs/private-data.md).

``scale guard install`` writes two hooks into a clone (of this framework repository, or of a data repository made by
``scale data init``):

* **pre-commit** (``scale guard check --staged``) inspects the snapshot the commit will record, the git index's blobs,
  not the working tree: a file changed after ``git add`` is judged as it was added.
* **pre-push** (``scale guard check --push REMOTE``, reading git's pre-push lines) inspects every commit the push
  sends that the remote does not hold, one by one, so a private file added in one commit and deleted in a later one is
  still found; in a data repository it also refuses a remote its policy does not list.

What a snapshot may hold (:func:`check_snapshot`):

* **scale-catal documents** (a JSON or YAML file whose ``graph.schema`` is scale-catal, and every graph kept as parts,
  rebuilt from its ``parts/`` blobs) that every reader of the repository may read: in the framework repository each is
  public, explicitly (``rights.doc_access_problems``, the rule of ``scale evidence access``); in a data repository the
  repository's readers (its ``scale-data.yaml``) are a subset of each record's (``audience.audience_problems``). A
  document or a graph's parts that cannot be read are refused as malformed. Quoted text stays within the copyright
  cap of the nearest ``rights.yaml`` (``rights.doc_copyright_problems``; a data repository may turn this off).
* **no undeclared raw data**: a file whose name says table, array, database, archive, instrument or simulation output
  or document (.csv, .xlsx, .h5, .parquet, .zip, .pdf, .jsonl, OUTCAR ...), whose first bytes say so whatever its
  name (a renamed archive), a text file laid out as a table, or any file over 5 MiB, unless a reviewed pattern
  allows it: ``governance/public-fixtures.yaml`` in the framework repository, ``repository.raw_data`` in a data
  repository's policy.

A repository with neither the framework's schema nor a ``scale-data.yaml`` is held to the framework's rules (public).

**Limits.** Hooks prevent accidents, not intent: ``git commit --no-verify`` and ``git push --no-verify`` skip them,
a clone made elsewhere has none until installed, and they see only what git is asked to record. Isolation (no network
and no credentials for public remotes where private data is processed) is the boundary; CI is a later line.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml

from . import graphparts, layout, rights
from .audience import POLICY_FILE, audience_problems, parse_policy
from .util import ScaleError

ZERO = "0" * 40
LARGE = 5 * 1024 * 1024
HOOK_MARK = "# scale guard hook"
CHAINED = ".scale-chained"
FRAMEWORK_MARKERS = (layout.KERNEL, layout.DOMAIN_DECLARATION)

# ---------------------------------------------------------------- raw data
RAW_EXT = {
    **dict.fromkeys((".csv", ".tsv", ".tab", ".xlsx", ".xlsm", ".xls", ".ods", ".parquet", ".feather", ".arrow",
                     ".orc", ".avro"), "a table"),
    **dict.fromkeys((".h5", ".hdf5", ".hdf", ".he5", ".nc", ".cdf", ".npy", ".npz", ".mat", ".pkl", ".pickle",
                     ".joblib", ".rds", ".rdata", ".sav", ".dta", ".dat", ".bin"), "a data array"),
    **dict.fromkeys((".db", ".sqlite", ".sqlite3", ".duckdb", ".mdb", ".accdb"), "a database"),
    **dict.fromkeys((".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".7z", ".rar", ".zst", ".lz4",
                     ".lzma"), "an archive"),
    **dict.fromkeys((".jsonl", ".ndjson"), "a record dump (an evidence index holds quotes)"),
    **dict.fromkeys((".pdf", ".docx", ".doc", ".pptx", ".odt", ".rtf"), "a document (a source text?)"),
    **dict.fromkeys((".cif", ".xyz", ".vasp", ".traj", ".cube", ".chk", ".fchk", ".poscar", ".outcar", ".xrdml",
                     ".jdx", ".dx", ".spc", ".mzml", ".fid", ".raw", ".sp", ".csvy"), "instrument or simulation output"),
}
RAW_NAMES = {n: "simulation output" for n in ("outcar", "poscar", "contcar", "oszicar", "wavecar", "chgcar", "doscar",
                                              "eigenval", "procar", "xdatcar", "vasprun.xml", "locpot")}
MAGIC = (
    (0, b"PK\x03\x04", "a zip archive (an xlsx, docx or jar is one)"), (0, b"PK\x05\x06", "a zip archive"),
    (0, b"\x1f\x8b", "a gzip archive"), (0, b"\xfd7zXZ\x00", "an xz archive"), (0, b"7z\xbc\xaf\x27\x1c", "a 7z archive"),
    (0, b"Rar!\x1a\x07", "a rar archive"), (0, b"\x28\xb5\x2f\xfd", "a zstd archive"), (0, b"\x04\x22\x4d\x18", "an lz4 archive"),
    (0, b"\x89HDF\r\n\x1a\n", "an HDF5 file"), (512, b"\x89HDF\r\n\x1a\n", "an HDF5 file"),
    (0, b"SQLite format 3\x00", "an SQLite database"), (0, b"\x93NUMPY", "a NumPy array"), (0, b"%PDF-", "a PDF"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an OLE2 file (xls, doc)"), (0, b"CDF\x01", "a netCDF file"),
    (0, b"CDF\x02", "a netCDF file"), (0, b"ARROW1", "an Arrow or Feather file"), (257, b"ustar", "a tar archive"),
)
SNIFF_EXT = {"", ".txt", ".out", ".log", ".text", ".asc"}     # text whose layout is read: a table is raw data


def raw_kind(path: str, data: bytes) -> str | None:
    """What raw data a file is, by its name or its first bytes (None: not raw data by these rules)."""
    name = PurePosixPath(path).name.lower()
    ext = PurePosixPath(name).suffix
    for off, sig, kind in MAGIC:
        if data[off:off + len(sig)] == sig:
            return kind
    if data[:3] == b"BZh" and data[3:4] in b"123456789" and len(data) > 4:
        return "a bzip2 archive"
    if data[:4] == b"PAR1" and data[-4:] == b"PAR1":
        return "a Parquet file"
    if data[:1] == b"\x80" and data[1:2] in (b"\x02", b"\x03", b"\x04", b"\x05"):
        return "a Python pickle"
    if name in RAW_NAMES:
        return RAW_NAMES[name]
    if ext in RAW_EXT:
        return RAW_EXT[ext]
    if ext in SNIFF_EXT and _tabular(data):
        return "a table (text laid out in columns)"
    return None


def _tabular(data: bytes) -> bool:
    """Text whose first lines (at least ten) nearly all hold the same number, one or more, of one delimiter."""
    try:
        text = data[:65536].decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()][:200]
    if len(lines) < 10:
        return False
    for d in (",", "\t", ";", "|"):
        counts = [ln.count(d) for ln in lines]
        top = max(set(counts), key=counts.count)
        if top >= 1 and counts.count(top) >= 0.9 * len(counts):
            return True
    return False


def _glob(pattern: str) -> re.Pattern:
    """A path pattern: ``**`` any run of folders, ``*`` and ``?`` within one name."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def allowed(path: str, patterns: list[str]) -> bool:
    return any(_glob(p).match(path) for p in patterns)


# ---------------------------------------------------------------- git
def _git(cwd: Path, *args: str, input: bytes | None = None, check: bool = True) -> bytes:
    r = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True)
    if check and r.returncode != 0:
        raise ScaleError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def toplevel(path: str | Path | None = None) -> Path:
    p = Path(path or ".").resolve()
    r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        raise ScaleError(f"{p}: not inside a git repository")
    return Path(r.stdout.strip())


class Blobs:
    """Blob contents by object id, read through one ``git cat-file --batch``."""

    def __init__(self, repo: Path):
        self.proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)
        self.cache: dict[str, bytes] = {}

    def read(self, sha: str) -> bytes:
        if sha not in self.cache:
            self.proc.stdin.write(sha.encode() + b"\n")
            self.proc.stdin.flush()
            head = self.proc.stdout.readline().split()
            if len(head) < 3 or head[1] == b"missing":
                raise ScaleError(f"git object {sha} is missing")
            data = self.proc.stdout.read(int(head[2]))
            self.proc.stdout.read(1)
            self.cache[sha] = data
        return self.cache[sha]

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def _entries(raw: bytes, index: bool) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob id)} from ``git ls-files -s -z`` (index) or ``git ls-tree -r -z`` output."""
    out = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.decode("utf-8", "surrogateescape").partition("\t")
        f = meta.split()
        if index:
            if f[2] != "0":                       # an unmerged path: its stages are not a snapshot
                continue
            out[path] = (f[0], f[1])
        elif f[1] == "blob":
            out[path] = (f[0], f[2])
    return out


def index_entries(repo: Path) -> dict[str, tuple[str, str]]:
    return _entries(_git(repo, "ls-files", "-s", "-z"), True)


def tree_entries(repo: Path, rev: str | None) -> dict[str, tuple[str, str]]:
    if rev is None:
        return {}
    return _entries(_git(repo, "ls-tree", "-r", "-z", rev), False)


def _head(repo: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


class Snapshot:
    """The files of one snapshot (the index, or a commit's tree): paths, modes and contents from git objects."""

    def __init__(self, entries: dict[str, tuple[str, str]], blobs: Blobs, label: str):
        self.entries, self.blobs, self.label = entries, blobs, label

    def has(self, path: str) -> bool:
        return path in self.entries

    def data(self, path: str) -> bytes:
        return self.blobs.read(self.entries[path][1])

    def regular(self, path: str) -> bool:
        return self.entries[path][0] in ("100644", "100755")


# ---------------------------------------------------------------- the rules
def _problem(rule: str, path: str, message: str) -> dict:
    return {"rule": rule, "path": path, "message": message}


def _yaml(data: bytes):
    return yaml.load(data.decode("utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


# a YAML file that may be a scale-catal document (a graph object naming the schema); other YAML is not parsed
YAML_DOC = re.compile(rb"^[ \t]*schema:[ \t]*[\"']?scale-(?:catal|graph)[\"']?[ \t]*(?:#.*)?$", re.M)


class Rules:
    """What a snapshot is held to: the framework's rules (public) or a data repository's policy."""

    def __init__(self, snap: Snapshot):
        self.problems: list[dict] = []
        self.framework = all(snap.has(m) for m in FRAMEWORK_MARKERS)
        self.policy = None
        self.kind = "framework" if self.framework else "unknown"
        if snap.has(POLICY_FILE):
            if self.framework:
                self.problems.append(_problem("policy", POLICY_FILE, f"a {POLICY_FILE} in the SCALE framework repository: "
                                              "data repositories are repositories of their own (scale data init)"))
            else:
                try:
                    self.policy = parse_policy(_yaml(snap.data(POLICY_FILE)), POLICY_FILE)
                    self.kind = "data"
                except (ScaleError, ValueError, yaml.YAMLError) as exc:
                    self.problems.append(_problem("policy", POLICY_FILE, f"the policy cannot be read ({exc}); the "
                                                  "repository is held to public rules until it is fixed"))
        self.allow: list[str] = list(self.policy["raw_data"]) if self.policy else []
        if self.policy is None and snap.has(layout.PUBLIC_FIXTURES):
            try:
                data = _yaml(snap.data(layout.PUBLIC_FIXTURES)) or {}
                self.allow = [p for f in data.get("fixtures") or [] for p in f.get("paths") or []]
            except (ValueError, yaml.YAMLError, AttributeError, TypeError) as exc:
                self.problems.append(_problem("policy", layout.PUBLIC_FIXTURES, f"cannot be read: {exc}"))
        self.cap = self.policy["copyright_cap"] if self.policy else True

    def readers_text(self) -> str:
        return "the public" if self.policy is None else json.dumps(self.policy["readers"], sort_keys=True)

    def audience(self, doc: dict, rel: str) -> list[str]:
        if self.policy is None:
            return rights.doc_access_problems(doc, rel)
        return audience_problems(doc, rel, self.policy["readers"])


def _parts_folder(path: str) -> str | None:
    """The graph folder a parts file belongs to (``<folder>/parts/header.json``, ``shared.json``,
    ``sources/<slug>.json``)."""
    p = PurePosixPath(path)
    if p.suffix != ".json":
        return None
    if len(p.parts) >= 2 and p.parts[-2] == graphparts.PARTS and p.name in (graphparts.HEADER, graphparts.SHARED):
        return str(p.parent.parent)
    if len(p.parts) >= 3 and p.parts[-3:-1] == (graphparts.PARTS, graphparts.SOURCES):
        return str(p.parent.parent.parent)
    return None


def _rights_for(snap: Snapshot, folder: str, rules: Rules) -> tuple[dict | None, str | None]:
    d = PurePosixPath(folder)
    for cand in [d, *d.parents]:
        rel = str(cand / rights.RIGHTS_FILE) if str(cand) != "." else rights.RIGHTS_FILE
        if snap.has(rel):
            try:
                return rights.parse_rights(_yaml(snap.data(rel)), rel), rel
            except (ScaleError, ValueError, yaml.YAMLError) as exc:
                rules.problems.append(_problem("copyright", rel, f"the rights file cannot be read: {exc}"))
                return {}, rel
    return None, None


def _check_doc(snap: Snapshot, doc, rel: str, folder: str, rules: Rules) -> None:
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list) or not isinstance(doc.get("links"), list) \
            or not all(isinstance(r, dict) for r in doc["nodes"] + doc["links"]):
        rules.problems.append(_problem("malformed", rel, "a scale-catal document needs a graph object and lists of node "
                                       "and link objects; it cannot be checked, so it is refused"))
        return
    for m in rules.audience(doc, rel):
        rules.problems.append(_problem("access" if rules.policy is None else "audience", rel,
                                       f"{m}; this repository's readers are {rules.readers_text()}"))
    if rules.cap:
        entries, rf = _rights_for(snap, folder, rules)
        _, found = rights.doc_copyright_problems(doc, rel, entries, rf)
        rules.problems += [_problem("copyright", rel, m) for m in found]


def _is_scale(doc) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("graph"), dict) and \
        doc["graph"].get("schema") in (layout.SCHEMA_NAME, layout.LEGACY_SCHEMA_NAME)


def check_snapshot(snap: Snapshot, changed: set[str] | None = None) -> list[dict]:
    """The problems of a snapshot: of the ``changed`` paths (None: every path), and of the documents they touch (a
    changed part rebuilds its graph; a changed rights file re-checks the documents below it; a changed policy or
    fixture list re-checks everything)."""
    rules = Rules(snap)
    present = set(snap.entries)
    if changed is None or {POLICY_FILE, layout.PUBLIC_FIXTURES} & changed:
        targets = set(present)
    else:
        targets = set(changed) & present
        for p in list(targets):
            if PurePosixPath(p).name == rights.RIGHTS_FILE:
                base = str(PurePosixPath(p).parent)
                targets |= {q for q in present if base == "." or q.startswith(base + "/")}
    folders: set[str] = set()
    for path in sorted(targets):
        if not snap.regular(path):                    # a symbolic link or a submodule: no content of its own here
            continue
        data = snap.data(path)
        if not allowed(path, rules.allow):
            kind = raw_kind(path, data)
            if kind:
                rules.problems.append(_problem("raw_data", path, f"{kind}: raw data is never committed here unless a "
                                               "reviewed pattern allows it (" + ("repository.raw_data in " + POLICY_FILE
                                               if rules.policy is not None else layout.PUBLIC_FIXTURES) + ")"))
                continue
            if len(data) > LARGE:
                rules.problems.append(_problem("raw_data", path, f"{len(data) // (1024 * 1024)} MiB: a file this large "
                                               "is taken for raw data unless a reviewed pattern allows it"))
                continue
        folder = _parts_folder(path)
        if folder is not None:
            folders.add(folder)
            continue
        suffix = PurePosixPath(path).suffix
        if suffix not in (".json", ".yaml", ".yml") or b"scale-catal" not in data and b"scale-graph" not in data \
                or suffix != ".json" and not YAML_DOC.search(data):
            continue
        try:
            doc = json.loads(data.decode("utf-8")) if suffix == ".json" else _yaml(data)
        except (ValueError, yaml.YAMLError) as exc:
            if suffix == ".json":
                rules.problems.append(_problem("malformed", path, f"names scale-catal but is not JSON ({exc}); "
                                               "it cannot be checked, so it is refused"))
            continue
        if _is_scale(doc):
            _check_doc(snap, doc, path, str(PurePosixPath(path).parent), rules)
    for folder in sorted(folders):
        _check_parts(snap, folder, present, rules)
    return rules.problems


def _check_parts(snap: Snapshot, folder: str, present: set[str], rules: Rules) -> None:
    base = f"{folder}/{graphparts.PARTS}/" if folder != "." else f"{graphparts.PARTS}/"
    header = base + graphparts.HEADER
    if header not in present:
        rules.problems.append(_problem("malformed", base, "graph parts without parts/header.json: the graph cannot be "
                                       "built, so its parts are refused"))
        return
    try:
        load = lambda p: json.loads(snap.data(p).decode("utf-8"))      # noqa: E731
        parts = {"header": load(header),
                 "shared": load(base + graphparts.SHARED) if base + graphparts.SHARED in present else
                 {"nodes": [], "links": []},
                 "sources": {PurePosixPath(p).stem: load(p) for p in sorted(present)
                             if p.startswith(base + graphparts.SOURCES + "/") and p.endswith(".json")
                             and p.count("/") == base.count("/") + 1}}
        doc = graphparts.build(parts)
    except (ValueError, ScaleError, KeyError, TypeError, AttributeError) as exc:
        rules.problems.append(_problem("malformed", base, f"the graph's parts do not build ({str(exc)[:300]}); "
                                       "it cannot be checked, so they are refused"))
        return
    if _is_scale(doc):
        _check_doc(snap, doc, folder, folder, rules)
    else:
        rules.problems.append(_problem("malformed", header, "a graph kept as parts whose header is not a scale-catal "
                                       "document"))


# ---------------------------------------------------------------- entry points
def check_staged(repo: Path) -> dict:
    """The snapshot a commit would record (the index), against HEAD's: the changed paths and what they touch."""
    blobs = Blobs(repo)
    try:
        index, head = index_entries(repo), tree_entries(repo, _head(repo))
        changed = {p for p, e in index.items() if head.get(p) != e}
        problems = check_snapshot(Snapshot(index, blobs, "the staged snapshot"), changed)
    finally:
        blobs.close()
    return {"checked": "staged", "paths": len(changed), "problems": problems, "ok": not problems}


def check_all(repo: Path, rev: str | None = None) -> dict:
    """Every path of the index (or of a commit, ``rev``)."""
    blobs = Blobs(repo)
    try:
        entries = tree_entries(repo, rev) if rev else index_entries(repo)
        problems = check_snapshot(Snapshot(entries, blobs, rev or "the index"), None)
    finally:
        blobs.close()
    return {"checked": rev or "index", "paths": len(entries), "problems": problems, "ok": not problems}


def _norm_url(url: str) -> str:
    u = url.strip().rstrip("/")
    return (u[:-4] if u.endswith(".git") else u).lower()


def _exists(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def parse_push_lines(text: str) -> list[tuple[str, str, str, str]]:
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) == 4:
            out.append((f[0], f[1], f[2], f[3]))
    return out


def check_push(repo: Path, remote: str, url: str | None = None, lines: list[tuple] | None = None,
               rng: str | None = None) -> dict:
    """Every commit a push sends that ``remote`` does not hold (git's pre-push ``lines``, or a revision range
    ``rng``), each judged on its own snapshot against its first parent's, and the destination: a data repository
    pushes only to a remote its policy lists."""
    if url is None:
        url = _git(repo, "remote", "get-url", remote, check=False).decode().strip() or remote
    plans: list[tuple[str, list[str]]] = []          # (tip, rev-list arguments)
    if rng is not None:
        tip = rng.split("..")[-1] if ".." in rng else rng
        plans.append((tip or "HEAD", [rng] if ".." in rng else [rng, "--not", f"--remotes={remote}"]))
    for _, local, _, remote_sha in lines or []:
        if local == ZERO:                            # a deletion sends no objects
            continue
        excl = ["--not", f"--remotes={remote}"] + ([remote_sha] if remote_sha != ZERO and _exists(repo, remote_sha) else [])
        plans.append((local, [local, *excl]))
    blobs, trees, problems, commits = Blobs(repo), {}, [], []

    def tree(rev):
        if rev not in trees:
            trees[rev] = tree_entries(repo, rev)
        return trees[rev]
    try:
        for tip, args in plans:
            revs = _git(repo, "rev-list", "--reverse", "--parents", *args).decode().split("\n")
            for line in filter(None, revs):
                c, *parents = line.split()
                if c in commits:
                    continue
                commits.append(c)
                now, before = tree(c), tree(parents[0]) if parents else {}
                changed = {p for p, e in now.items() if before.get(p) != e}
                for pr in check_snapshot(Snapshot(now, blobs, c), changed):
                    problems.append({**pr, "commit": c})
            rules = Rules(Snapshot(tree(tip), blobs, tip))
            if rules.policy is not None and _norm_url(url) not in {_norm_url(u) for u in rules.policy["remotes"]}:
                problems.append({"rule": "destination", "path": POLICY_FILE, "commit": tip,
                                 "message": f"{url} is not among the remotes this data repository's policy lists "
                                            "(repository.remotes): add it only once you have checked that its readers "
                                            "are the repository's readers"})
    finally:
        blobs.close()
    return {"checked": "push", "remote": remote, "url": url, "commits": len(commits), "problems": problems,
            "ok": not problems}


def report_text(res: dict) -> str:
    if res["ok"]:
        what = f"{res['commits']} outgoing commits" if res["checked"] == "push" else f"{res['paths']} paths"
        return f"scale guard: {what} checked, nothing refused"
    lines = [f"scale guard: refused ({len(res['problems'])} problems):"]
    for p in res["problems"][:50]:
        lines.append(f"  [{p['rule']}] {p['path']}{' @ ' + p['commit'][:10] if p.get('commit') else ''}: {p['message']}")
    if len(res["problems"]) > 50:
        lines.append(f"  … and {len(res['problems']) - 50} more")
    lines.append("Unstage or remove what is refused (a commit already made: rewrite it before pushing). The guard "
                 "prevents accidents only; docs/private-data.md says what it does not catch.")
    return "\n".join(lines)


# ---------------------------------------------------------------- hooks
HOOKS = {
    "pre-commit": ('run_scale guard check --staged || exit 1\n'
                   'if [ -x "$0' + CHAINED + '" ]; then exec "$0' + CHAINED + '" "$@"; fi\n'),
    "pre-push": ('input=$(cat)\n'
                 'printf \'%s\\n\' "$input" | run_scale guard check --push "$1" --url "$2" || exit 1\n'
                 'if [ -x "$0' + CHAINED + '" ]; then printf \'%s\\n\' "$input" | "$0' + CHAINED + '" "$@"; exit $?; fi\n'),
}


def hook_text(name: str, python: str) -> str:
    py = shlex.quote(python)
    return (f"#!/bin/sh\n{HOOK_MARK}: {name} (scale guard install; docs/private-data.md)\n"
            "# Refuses what this repository's readers may not read, malformed scale-catal documents, quoted text beyond\n"
            "# the copyright cap and undeclared raw data. It prevents accidents only: --no-verify skips it, and\n"
            "# isolation (no network, no credentials for public remotes) is the boundary.\n"
            "run_scale() {\n"
            f"  if [ -x {py} ] && {py} -c 'import scale.guard' >/dev/null 2>&1; then {py} -m scale.cli \"$@\"\n"
            "  elif command -v scale >/dev/null 2>&1; then scale \"$@\"\n"
            "  else echo \"scale guard: cannot find scale (the Python that installed this hook is gone and scale is not "
            "on PATH); reinstall the hooks with scale guard install. Nothing was let through.\" >&2; return 3\n"
            "  fi\n}\n" + HOOKS[name])


def hooks_dir(repo: Path) -> Path:
    custom = _git(repo, "config", "--get", "core.hooksPath", check=False).decode().strip()
    if custom:
        p = Path(custom).expanduser()
        return p if p.is_absolute() else repo / p
    p = Path(_git(repo, "rev-parse", "--git-path", "hooks").decode().strip())
    return p if p.is_absolute() else repo / p


def install(repo: Path, force: bool = False, python: str | None = None) -> dict:
    """Write the pre-commit and pre-push hooks. A hook of another origin is kept and run after the guard
    (``<hook>.scale-chained``) with ``force``; without it, the install stops and changes nothing."""
    d = hooks_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    foreign = [n for n in HOOKS if (d / n).exists() and HOOK_MARK not in (d / n).read_text(errors="replace")]
    if foreign and not force:
        raise ScaleError(f"{d}: {', '.join(foreign)} already exist and are not the guard's; --force keeps each as "
                         f"<hook>{CHAINED} and runs it after the guard")
    written = []
    for name in HOOKS:
        p = d / name
        if name in foreign:
            chained = d / (name + CHAINED)
            if chained.exists():
                raise ScaleError(f"{chained} exists already; move it away first")
            p.rename(chained)
        p.write_text(hook_text(name, python or sys.executable), encoding="utf-8")
        p.chmod(0o755)
        written.append(str(p))
    return {"hooks": written, "chained": [str(d / (n + CHAINED)) for n in foreign],
            "note": "hooks prevent accidents only: --no-verify skips them; see docs/private-data.md"}


__all__ = ["raw_kind", "allowed", "check_snapshot", "check_staged", "check_all", "check_push", "install", "hooks_dir",
           "hook_text", "report_text", "parse_push_lines", "toplevel"]
