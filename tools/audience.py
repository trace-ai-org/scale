"""Who may read what: a data repository's policy, its readers against each record's, and the access levels a payload
carries before it leaves the machine (docs/private-data.md).

A **data repository** (``scale data init``) keeps project data apart from this framework repository. Its policy file,
``scale-data.yaml`` at its root, says who reads the repository and which model endpoints may receive which access
levels::

    scale_data_policy: 1
    repository:
      readers: {access: project, project: <project id>}   # stewardship keys: access, project, contributed_by
      remotes: []            # remote URLs whose readers are checked to be the readers above (none: no push)
      raw_data: []           # path patterns of raw-data files the repository may track (none by default)
      copyright_cap: true    # quoted text within the copyright cap (rights.yaml), as in the framework
    defaults:
      stewardship: {access: project, project: <project id>}  # what a new document of the repository states
    egress:
      endpoints:             # deny by default: a non-public payload goes only to an endpoint listed for its level
      - {id: local, url: http://localhost:11434, access: [consortium, project, institution]}

**The audience rule.** Every reader of the repository must be allowed to read every artifact in it: the repository's
readers are a subset of each record's permitted readers. A record is checked as ``scale graph filter`` reads it
(``pgraph.effective_stewardship``, an embargo in force read as institution): it fails when some least-privileged reader
of the repository (``pgraph.audience``) may not see it (``pgraph.less_open``), unless its stewardship names exactly the
repository's own audience. Public data in a project repository passes; institution-only data in a project repository,
or one project's data in another project's repository, does not. The framework repository's readers are the public.

**Egress.** A payload's levels are the access levels its document's header and records grant today
(:func:`payload_levels`); a public payload may go to any endpoint, any other level only to an endpoint the trusted
policy lists for it (:mod:`scale.egress`).
"""

from __future__ import annotations

from pathlib import Path

from . import pgraph
from .util import ScaleError, load_yaml

POLICY_FILE = "scale-data.yaml"
POLICY_VERSION = 1
LEVELS = tuple(pgraph.OPENNESS)                    # public, consortium, project, institution
READER_KEYS = ("access", "project", "contributed_by")
_TOP = {"scale_data_policy", "repository", "defaults", "egress"}
_REPO = {"readers", "remotes", "raw_data", "copyright_cap"}


def _strings(v, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ScaleError(f"{where} is a list of texts")
    return list(v)


def check_readers(readers, where: str) -> dict:
    """A declared audience (stewardship keys access, project, contributed_by), checked: a level of
    :data:`LEVELS`, the project of a project audience, the institutions of an institution audience."""
    if not isinstance(readers, dict) or set(readers) - set(READER_KEYS):
        raise ScaleError(f"{where} holds access and, as the level needs, project or contributed_by")
    if readers.get("access") not in LEVELS:
        raise ScaleError(f"{where}.access is one of {', '.join(LEVELS)} (an embargo is a record's, not a repository's)")
    if readers["access"] == "project" and not (isinstance(readers.get("project"), str) and readers["project"].strip()):
        raise ScaleError(f"{where}: a project audience names its project (project: <id>)")
    if readers["access"] == "institution" and not _strings(readers.get("contributed_by"), f"{where}.contributed_by"):
        raise ScaleError(f"{where}: an institution audience names its institutions (contributed_by: [<id>, ...])")
    return dict(readers)


def parse_policy(data, where: str = POLICY_FILE) -> dict:
    """A data repository's policy, checked (unknown keys are refused, so a misspelt key never loosens it):
    ``{readers, remotes, raw_data, copyright_cap, defaults, egress: [{id, url, access}]}``."""
    if not isinstance(data, dict) or set(data) - _TOP:
        raise ScaleError(f"{where}: a policy holds {', '.join(sorted(_TOP))}")
    if data.get("scale_data_policy") != POLICY_VERSION:
        raise ScaleError(f"{where}: scale_data_policy: {POLICY_VERSION} is required (the policy format's version)")
    repo = data.get("repository")
    if not isinstance(repo, dict) or set(repo) - _REPO:
        raise ScaleError(f"{where}: repository holds {', '.join(sorted(_REPO))}")
    readers = check_readers(repo.get("readers"), f"{where}: repository.readers")
    cap = repo.get("copyright_cap", True)
    if not isinstance(cap, bool):
        raise ScaleError(f"{where}: repository.copyright_cap is true or false")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict) or set(defaults) - {"stewardship"}:
        raise ScaleError(f"{where}: defaults holds stewardship")
    egress = data.get("egress") or {}
    if not isinstance(egress, dict) or set(egress) - {"endpoints"}:
        raise ScaleError(f"{where}: egress holds endpoints (anything not listed is denied)")
    return {"readers": readers, "remotes": _strings(repo.get("remotes"), f"{where}: repository.remotes"),
            "raw_data": _strings(repo.get("raw_data"), f"{where}: repository.raw_data"), "copyright_cap": cap,
            "defaults": defaults, "endpoints": parse_endpoints(egress.get("endpoints"), f"{where}: egress.endpoints")}


def parse_endpoints(endpoints, where: str) -> list[dict]:
    """Egress endpoints: ``[{id, url?, access: [levels]}]``; an id once, each level one of :data:`LEVELS`."""
    if endpoints is None:
        return []
    if not isinstance(endpoints, list):
        raise ScaleError(f"{where} is a list of {{id, url, access}}")
    out, seen = [], set()
    for i, e in enumerate(endpoints):
        w = f"{where}[{i}]"
        if not isinstance(e, dict) or set(e) - {"id", "url", "access", "note"}:
            raise ScaleError(f"{w} holds id, url, access and (optional) note")
        if not isinstance(e.get("id"), str) or not e["id"].strip() or e["id"] in seen:
            raise ScaleError(f"{w}.id is a text, given once")
        seen.add(e["id"])
        levels = _strings(e.get("access"), f"{w}.access")
        if set(levels) - set(LEVELS):
            raise ScaleError(f"{w}.access lists levels among {', '.join(LEVELS)}")
        out.append({"id": e["id"], "url": e.get("url"), "access": sorted(set(levels), key=LEVELS.index)})
    return out


def load_policy(path: str | Path) -> dict:
    return parse_policy(load_yaml(Path(path)), str(path))


def find_policy(start: str | Path) -> Path | None:
    """The policy file of the data repository holding ``start``: the nearest ``scale-data.yaml`` in its folder or
    above."""
    p = Path(start).expanduser().resolve()
    for d in [p if p.is_dir() else p.parent, *(p if p.is_dir() else p.parent).parents]:
        if (d / POLICY_FILE).is_file():
            return d / POLICY_FILE
    return None


# ---------------------------------------------------------------- the audience rule
def _describe(st: dict, today: str) -> str:
    level, who = pgraph.access_key(st, today)
    if level == "project":
        return f"project {who}" if who else "a project it does not name"
    if level == "institution":
        return f"institution {', '.join(who)}" if who else "no institution it names"
    return level


def audience_problems(doc: dict, rel: str, readers: dict, today: str | None = None) -> list[str]:
    """The records of a document (and its header) that some reader of the repository may not read (the module's
    audience rule), grouped by who may read them; ``readers`` is the repository's declared audience."""
    today = today or pgraph.embargo_today()
    viewers, own = pgraph.audience(doc, readers, today), pgraph.access_key(readers, today)
    graph = doc.get("graph") if isinstance(doc.get("graph"), dict) else {}
    header = pgraph.stewardship_of(graph.get("stewardship"))

    def narrower(st: dict) -> bool:
        return pgraph.access_key(st, today) != own and pgraph.less_open(st, viewers, today)
    out = []
    if narrower(header):
        out.append(f"{rel}: the graph's stewardship admits {_describe(header, today)}, fewer readers than this "
                   f"repository's ({_describe(readers, today)})")
    groups: dict[str, list] = {}
    for r in list(doc.get("nodes") or []) + list(doc.get("links") or []):
        st = pgraph.effective_stewardship(doc, r)
        if narrower(st):
            why = _describe(st, today) + (" (an embargo in force)" if st.get("access") == "embargoed" else "")
            groups.setdefault(why, []).append(r.get("id"))
    for why, ids in sorted(groups.items()):
        out.append(f"{rel}: {len(ids)} records may be read only by {why}, fewer readers than this repository's "
                   f"({_describe(readers, today)}) (e.g. {ids[0]})")
    return out


# ---------------------------------------------------------------- egress
def payload_levels(doc: dict, today: str | None = None) -> set[str]:
    """The access levels a document's header and records grant today (``pgraph.openness``: a missing or malformed
    access is institution, an embargo in force institution): what any payload drawn from it may carry."""
    today = today or pgraph.embargo_today()
    if not isinstance(doc, dict):
        return {"institution"}
    graph = doc.get("graph") if isinstance(doc.get("graph"), dict) else {}
    levels = {pgraph.openness(pgraph.stewardship_of(graph.get("stewardship")), today)}
    for r in list(doc.get("nodes") or []) + list(doc.get("links") or []):
        levels.add(pgraph.openness(pgraph.effective_stewardship(doc, r), today) if isinstance(r, dict) else "institution")
    return levels


__all__ = ["POLICY_FILE", "LEVELS", "parse_policy", "load_policy", "find_policy", "parse_endpoints", "check_readers",
           "audience_problems", "payload_levels"]
