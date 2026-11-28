"""``scale data init DIR``: a private data repository of one's own, apart from the SCALE framework repository
(docs/private-data.md).

It writes a git repository with no remote, readable by its owner only (mode 0700), holding:

* ``README.md``: the model, in short (who reads the repository, what never enters it, what leaves it and how);
* ``scale-data.yaml``: the policy (:mod:`scale.audience`): the repository's readers, the remotes checked to have
  those readers (none), the raw-data patterns it may track (none), the copyright cap, the stewardship new documents
  state (access project by default) and the egress section, empty, so no model endpoint receives anything not public;
* ``rights.yaml``: the rights of the sources its documents quote (empty);
* ``.gitignore``: ``raw/``, ``index/``, ``exports/``, ``cache/``, ``work/`` and evidence indexes, which stay local;
* ``graphs/``: where the documents go, with one starter document (``graphs/lab.json``, :func:`new_document`) stating
  the policy's default stewardship; and the data guard's hooks (:mod:`scale.guard`), which hold every commit and push
  to the policy.

``scale data new DOC`` (:func:`new_document`) writes another empty, valid research_data document: the graph header
pinning the head schema version, its stewardship (from the arguments, else the ``defaults.stewardship`` of the data
repository holding DOC) and the Project and Institution records it names. :func:`repository_defaults` is what the other
writers of new documents read (``scale call template``, ``scale source start``), and :func:`refuse_framework` keeps
every writer of project data out of the SCALE framework repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import guard
from .audience import POLICY_FILE, check_readers, find_policy, parse_policy
from .util import ScaleError, load_yaml, write_canonical

LOCAL_DIRS = ("raw", "index", "exports", "cache", "work")

GITIGNORE = """# kept on this computer only (scale data init): raw data, indexes, exports, caches and working material.
# Ignoring a file keeps it out of git, not private: the folder's permissions and the machine keep it so.
/raw/
/index/
/exports/
/cache/
/work/
*.evidence.*
*.graph.json.bak
.DS_Store
"""

README = """# {title}

A private SCALE data repository (made by `scale data init`). It holds scale-catal documents of project data that
is not cleared for publication; it is not the SCALE framework repository, and nothing here goes there until the
project's partners clear it (docs/private-data.md in the SCALE repository).

## Who reads it

`scale-data.yaml` names the repository's readers (`repository.readers`): {readers}. Every reader of the repository
must be allowed to read every record in it, so a record is committed only when its stewardship admits at least
those readers. Public data may live here; data of one institution only, or of another project, may not.

Every new document states the stewardship in `defaults.stewardship` (in its graph header): {stewardship}.
`scale data new`, `scale add-source`, `scale call template` and `scale source start` write it when they run inside
this repository; the hooks refuse a document narrower than the readers but not one wider than you meant, so check a
document made elsewhere before you add it. `graphs/lab.json` is a starter document to add data and sources to
(`scale add-source FILE --into graphs/lab.json`).

## What stays on this computer

`raw/` (instrument files, simulation output, spreadsheets), `index/`, `exports/`, `cache/` and `work/` are ignored by
git. Documents cite raw data by a Dataset record (uri and sha256), never by copying it. A raw-data file is refused by
the hooks unless `repository.raw_data` lists a pattern for it.

## The hooks

`scale guard install` put two git hooks here: pre-commit checks the staged snapshot and pre-push every outgoing
commit, against `scale-data.yaml`, and refuse records narrower than the repository's readers, malformed documents,
quoted text beyond the copyright cap and undeclared raw data. The repository has no remote: a push is refused until
`repository.remotes` lists one whose readers you have checked to be the repository's readers. Hooks prevent
accidents only: `--no-verify` skips them, and a fresh clone has none until `scale guard install` runs in it.

## Models

The `egress` section lists the model endpoints that may receive records that are not public, by access level. It
is empty: a model reached through `scale mcp`, `scale serve` or `scale call` receives public records only. To approve
one, add `{{id, url, access: [project]}}` and start the tools with `SCALE_EGRESS_ENDPOINT=<id>`, once the project
agrees that the endpoint's provider may read its data.

## Limits

Ignored folders, checksums and the copyright cap do not make data confidential. Process private data where there is
no unrestricted network and no credentials for public repositories; the hooks and the egress gate catch mistakes
within that boundary.
"""


def _inside_git(path: Path) -> Path | None:
    p = path
    while not p.exists():
        p = p.parent
    r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def _yaml_list(xs: list[str]) -> str:
    return "[" + ", ".join(f'"{x}"' for x in xs) + "]"


def policy_text(readers: dict, defaults: dict | None = None) -> str:
    """The policy file's text: the readers, and the stewardship of new documents (``defaults``; the readers when
    none)."""
    dflt = defaults or readers
    lines = ["# The policy of this data repository (scale data init; docs/private-data.md in the SCALE repository).",
             "scale_data_policy: 1", "repository:",
             "  # who reads this repository: everyone with access to it, its remotes and its backups",
             "  readers:", f"    access: {readers['access']}"]
    if readers.get("project"):
        lines.append(f'    project: "{readers["project"]}"')
    if readers.get("contributed_by"):
        lines.append(f"    contributed_by: {_yaml_list(readers['contributed_by'])}")
    lines += ["  # remote URLs checked to have exactly these readers; a push anywhere else is refused",
              "  remotes: []",
              "  # path patterns of raw-data files this repository may track (** any folders, * one name)",
              "  raw_data: []",
              "  copyright_cap: true",
              "defaults:",
              "  # the stewardship of a new document of this repository (its graph header): scale data new, add-source,",
              "  # call template and source start write it here",
              "  stewardship:", f"    access: {dflt['access']}"]
    if dflt.get("project"):
        lines.append(f'    project: "{dflt["project"]}"')
    if dflt.get("contributed_by"):
        lines.append(f"    contributed_by: {_yaml_list(dflt['contributed_by'])}")
    lines += ["egress:",
              "  # model endpoints that may receive records that are not public, by level; anything unlisted is denied",
              "  # - id: local",
              "  #   url: http://localhost:11434",
              "  #   access: [project]",
              "  endpoints: []", ""]
    return "\n".join(lines)


def init(directory: str | Path, access: str = "project", project: str | None = None,
         institutions: list[str] | None = None, title: str | None = None, python: str | None = None,
         contributor: str | None = None) -> dict:
    """Scaffold a data repository at ``directory`` (new or empty, outside any git repository), with a starter document
    (``graphs/lab.json``) of the policy's default stewardship; ``contributor`` names who contributes its data (default:
    the first institution, else ``<prefix>:org/my-lab``)."""
    d = Path(directory).expanduser().resolve()
    if contributor is not None and not CURIE.match(contributor):
        raise ScaleError(f"--contributor {contributor!r}: an id with a prefix (lab:org/my-lab)")
    readers = {"access": access}
    if project:
        readers["project"] = project
    if institutions:
        readers["contributed_by"] = list(institutions)
    readers = check_readers(readers, "the repository's readers (--access, --project, --institution)")
    outer = _inside_git(d)
    if outer is not None:
        raise ScaleError(f"{d} is inside the git repository {outer}: a data repository is a repository of its own, "
                         "outside any other (above all outside the SCALE framework repository); choose a folder "
                         "elsewhere")
    if d.exists() and any(d.iterdir()):
        raise ScaleError(f"{d}: not empty; give a new or empty folder")
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    for sub in ("graphs", *LOCAL_DIRS):
        (d / sub).mkdir(exist_ok=True)
        (d / sub).chmod(0o700)
    who = {"project": f"the members of project {project}",
           "institution": f"the members of {', '.join(institutions or [])}",
           "consortium": "the members of the consortium's projects", "public": "anyone"}[access]
    # the stewardship of new documents: the readers' audience, naming the project and the contributor every
    # research_data document names (neither widens the audience: a project audience is its project's members)
    prefix = _prefix(project) or _prefix(contributor) or "lab"
    steward = contributor or (institutions or [None])[0] or f"{prefix}:org/my-lab"
    if access == "institution" and steward not in (institutions or []):
        raise ScaleError(f"--contributor {steward} is not among the repository's institutions (--institution)")
    defaults = {**readers, "project": readers.get("project") or f"{prefix}:project/{_slug(d.name)}",
                "contributed_by": readers.get("contributed_by") or [steward]}
    st = ", ".join(f"{k}: {v}" for k, v in defaults.items())
    files = {
        "README.md": README.format(title=title or f"{d.name}: a SCALE data repository", readers=who, stewardship=st),
        POLICY_FILE: policy_text(readers, defaults),
        "rights.yaml": "# The rights of the sources the documents of this repository quote (scale evidence report).\n"
                       "# sources:\n#   <source id>: {class: in_copyright, licence: <SPDX id or text>, basis: <why>}\n"
                       "sources: {}\n",
        ".gitignore": GITIGNORE,
        "graphs/README.md": "The scale-catal documents of this repository (a graph kept as parts is a folder with "
                            "parts/).\n",
    }
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    r = subprocess.run(["git", "init", "-q", str(d)], capture_output=True, text=True)
    if r.returncode != 0:
        raise ScaleError(f"git init: {r.stderr.strip()}")
    hooks = guard.install(d, python=python)
    starter = new_document(d / "graphs" / "lab.json", title=title or f"{d.name}: the laboratory's data")
    return {"repository": str(d), "readers": readers, "files": sorted([*files, "graphs/lab.json"]),
            "ignored": [f"{x}/" for x in LOCAL_DIRS], "hooks": hooks["hooks"], "remote": None,
            "starter": {"document": starter["written"], "stewardship": starter["stewardship"]},
            "next": [f"scale add-source <a data file, a PDF or a DOI> --into {starter['written']}",
                     "or make another document under graphs/ with scale data new graphs/<name>.json (it states "
                     "defaults.stewardship)",
                     f"the starter names the contributor {steward} (defaults.stewardship); give its Institution record "
                     "its name and kind if it is a placeholder (scale data init --contributor names it)",
                     "git add and git commit: the pre-commit hook checks the staged snapshot",
                     "add a remote only once its readers are the repository's, and list it in repository.remotes"]}


# ---------------------------------------------------------------- new documents
CURIE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:\S+$")


def _prefix(curie: str | None) -> str | None:
    return curie.split(":", 1)[0] if curie and CURIE.match(curie) else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "data"


def repository_defaults(where: str | Path | None) -> tuple[Path | None, dict]:
    """The policy file of the data repository holding ``where`` and the stewardship it gives new documents
    (``defaults.stewardship``; ``{}`` when it states none). Outside any data repository: ``(None, {})``."""
    if where is None:
        return None, {}
    pf = find_policy(where)
    if pf is None:
        return None, {}
    st = (parse_policy(load_yaml(pf), str(pf)).get("defaults") or {}).get("stewardship") or {}
    if not isinstance(st, dict):
        raise ScaleError(f"{pf}: defaults.stewardship is a stewardship (access, project, contributed_by)")
    return pf, dict(st)


def framework_root(path: str | Path) -> Path | None:
    """The SCALE framework repository holding ``path`` (a git repository with the framework's schema), or None."""
    p = Path(path).expanduser().resolve()
    top = _inside_git(p.parent if p.is_file() else p)
    if top is not None and all((top / m).is_file() for m in guard.FRAMEWORK_MARKERS):
        return top
    return None


def refuse_framework(path: str | Path, what: str = "project data") -> None:
    """Refuse to write ``what`` at ``path`` inside the SCALE framework repository, whose readers are the public."""
    top = framework_root(path)
    if top is not None:
        raise ScaleError(f"{path} is inside the SCALE framework repository ({top}), whose readers are the public: "
                         f"{what} never goes there. Make a data repository of your own (scale data init ~/data/<name> "
                         "--project <id>) and work in it")


def stewardship_for(path: str | Path, project: str | None = None, contributor: str | None = None,
                    access: str | None = None) -> tuple[dict, Path | None]:
    """The stewardship of a new document at ``path`` and the policy file of the data repository holding it: the
    arguments, else that repository's ``defaults.stewardship``, else access project. A contributor the defaults do not
    name is added, unless that would widen an institution audience."""
    pf, dflt = repository_defaults(path)
    st = {k: v for k, v in dflt.items() if v not in (None, "", [])}
    if access:
        st["access"] = access
    st.setdefault("access", "project")
    if project:
        st["project"] = project
    by = list(st.get("contributed_by") or [])
    if contributor and contributor not in by:
        if st["access"] == "institution" and by:
            raise ScaleError(f"{contributor} is not among the institutions the repository's documents admit "
                             f"({', '.join(by)}): naming it would widen who reads the document")
        by = [contributor, *by]
    if by:
        st["contributed_by"] = by
    for k, flag in (("project", "--project"), ("contributed_by", "--contributor")):
        if not st.get(k):
            raise ScaleError(f"a research_data document names its {k.replace('_', ' ')}: give {flag}"
                             + ("" if pf else " (or make it inside a data repository whose defaults.stewardship "
                                              "names it)"))
    for x in [st["project"], *st["contributed_by"]]:
        if not isinstance(x, str) or not CURIE.match(x):
            raise ScaleError(f"{x!r}: an id with a prefix (lab:project/my-project, lab:org/my-lab)")
    return st, pf


def _tail(curie: str) -> str:
    return curie.rsplit("/", 1)[-1].split(":")[-1]


def steward_records(st: dict, title: str | None = None) -> tuple[list[dict], list[dict]]:
    """The Institution and Project records a stewardship names, and each institution's PARTICIPATES_IN link, as
    ``scale source start`` writes them (so that a curated graph folds into the document without a conflict)."""
    nodes = [{"id": i, "label": "Institution", "props": {"name": _tail(i), "origin": "curated"}}
             for i in st["contributed_by"]]
    nodes.append({"id": st["project"], "label": "Project",
                  "props": {"name": title or _tail(st["project"]), "origin": "curated"}})
    links = [{"id": f"{st['project']}/participant/{_tail(i)}", "type": "PARTICIPATES_IN", "source": i,
              "target": st["project"], "props": {"origin": "curated", "role": "data_provider"}}
             for i in st["contributed_by"]]
    return nodes, links


def new_document(path: str | Path, project: str | None = None, contributor: str | None = None,
                 access: str | None = None, title: str | None = None, repo: Path | None = None) -> dict:
    """Write an empty, valid research_data document at ``path`` (a new .json file): the header pinning the head
    schema version, its stewardship (:func:`stewardship_for`) and the records it names. Refused inside the framework
    repository and, inside a data repository, when it would admit fewer readers than the repository's."""
    from . import pgraph
    from .audience import audience_problems
    from .util import find_repo_root
    p = Path(path).expanduser().resolve()
    if p.suffix != ".json":
        raise ScaleError(f"{path}: a document is a .json file")
    if p.exists():
        raise ScaleError(f"{path} exists; give a new file name")
    refuse_framework(p)
    st, pf = stewardship_for(p, project, contributor, access)
    spec = pgraph.load_spec(pgraph.spec_for(Path(repo) if repo else find_repo_root(), None, None, None))
    nodes, links = steward_records(st)
    doc = {"directed": True, "multigraph": True,
           "graph": {"schema": spec["schema"], "schema_version": spec["version"],
                     "graph_id": f"{_prefix(st['project']) or 'lab'}:graph/{_slug(p.stem)}", "title": title or p.stem,
                     "description": f"Research data of {st['project']}: records added by scale add-source (data files, "
                                    "publications, graphs), each with its evidence.",
                     "data_status": "research_data", "follows_domains": ["catalysis"], "stewardship": st},
           "nodes": nodes, "links": links}
    rep = pgraph.validate(doc, spec)
    if rep.errors:
        raise ScaleError("the new document would not validate: " + "; ".join(e["message"] for e in rep.errors[:5]))
    if pf is not None:
        problems = audience_problems(doc, p.name, parse_policy(load_yaml(pf), str(pf))["readers"])
        if problems:
            raise ScaleError("; ".join(problems) + ": the repository's hooks would refuse it")
    p.parent.mkdir(parents=True, exist_ok=True)
    write_canonical(p, doc)
    return {"written": str(p), "schema_version": spec["version"], "stewardship": st,
            "data_repository": str(pf.parent) if pf else None, "valid": True,
            "next": [f"scale add-source <a data file, a PDF or a DOI> --into {p}", f"scale graph validate {p}"]}


__all__ = ["init", "policy_text", "new_document", "repository_defaults", "refuse_framework", "framework_root",
           "stewardship_for", "steward_records"]
