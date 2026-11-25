"""``scale data init DIR``: a private data repository of one's own, apart from the SCALE framework repository
(docs/private-data.md).

It writes a git repository with no remote, readable by its owner only (mode 0700), holding:

* ``README.md``: the model, in short (who reads the repository, what never enters it, what leaves it and how);
* ``scale-data.yaml``: the policy (:mod:`scale.audience`): the repository's readers, the remotes checked to have
  those readers (none), the raw-data patterns it may track (none), the copyright cap, the stewardship new documents
  state (access project by default) and the egress section, empty, so no model endpoint receives anything not public;
* ``rights.yaml``: the rights of the sources its documents quote (empty);
* ``.gitignore``: ``raw/``, ``index/``, ``exports/``, ``cache/``, ``work/`` and evidence indexes, which stay local;
* ``graphs/``: where the documents go; and the data guard's hooks (:mod:`scale.guard`), which hold every commit and
  push to the policy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import guard
from .audience import POLICY_FILE, check_readers
from .util import ScaleError

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

Give every new document the stewardship in `defaults.stewardship` (in its graph header): {stewardship}. Set it
yourself: `scale call template` and `scale source start` write `access: public`, and the hooks refuse a document
narrower than the readers but not one wider than you meant.

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


def policy_text(readers: dict) -> str:
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
              "  # the stewardship to give a new document of this repository (in its graph header; set it yourself)",
              "  stewardship:", f"    access: {readers['access']}"]
    if readers.get("project"):
        lines.append(f'    project: "{readers["project"]}"')
    if readers.get("contributed_by"):
        lines.append(f"    contributed_by: {_yaml_list(readers['contributed_by'])}")
    lines += ["egress:",
              "  # model endpoints that may receive records that are not public, by level; anything unlisted is denied",
              "  # - id: local",
              "  #   url: http://localhost:11434",
              "  #   access: [project]",
              "  endpoints: []", ""]
    return "\n".join(lines)


def init(directory: str | Path, access: str = "project", project: str | None = None,
         institutions: list[str] | None = None, title: str | None = None, python: str | None = None) -> dict:
    """Scaffold a data repository at ``directory`` (new or empty, outside any git repository)."""
    d = Path(directory).expanduser().resolve()
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
    st = ", ".join(f"{k}: {v}" for k, v in readers.items())
    files = {
        "README.md": README.format(title=title or f"{d.name}: a SCALE data repository", readers=who, stewardship=st),
        POLICY_FILE: policy_text(readers),
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
    return {"repository": str(d), "readers": readers, "files": sorted(files), "ignored": [f"{x}/" for x in LOCAL_DIRS],
            "hooks": hooks["hooks"], "remote": None,
            "next": ["put documents under graphs/ with the stewardship in defaults.stewardship",
                     "git add and git commit: the pre-commit hook checks the staged snapshot",
                     "add a remote only once its readers are the repository's, and list it in repository.remotes"]}


__all__ = ["init", "policy_text"]
